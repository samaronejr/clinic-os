"""Service completion is the only automatic revenue-recognition event."""

RECOGNITION_SQL = """
CREATE FUNCTION clinic_app.billing_recognize_completion(
 requested_clinic uuid, requested_invoice uuid, evidence uuid)
RETURNS TABLE(entry_id uuid, changed boolean)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE terms clinic_app.billing_invoiceterms; invoice clinic_app.billing_invoice;
 book_id uuid; receivable text; prepaid numeric; refunded numeric; lines jsonb;
BEGIN
 SELECT t.book_id INTO book_id FROM clinic_app.billing_invoiceterms t
 JOIN clinic_app.billing_journalbook b ON b.id=t.book_id
 WHERE t.invoice_id=requested_invoice AND b.clinic_id=requested_clinic
 AND b.organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid;
 IF book_id IS NULL THEN
  RAISE EXCEPTION 'billing access denied' USING ERRCODE='42501';
 END IF;
 PERFORM clinic_app.billing_lock_book(book_id);
 SELECT * INTO terms FROM clinic_app.billing_invoiceterms
 WHERE invoice_id=requested_invoice;
 SELECT * INTO invoice FROM clinic_app.billing_invoice WHERE id=requested_invoice;
 IF NOT (clinic_app.billing_book_access(book_id,'settlement.post') OR EXISTS (
 SELECT 1 FROM clinic_app.scheduling_appointment a WHERE a.id=invoice.appointment_id
 AND a.practitioner_id=NULLIF(current_setting('app.current_user_id',true),'')::uuid
 AND a.status='completed'
 AND clinic_app.has_permission('appointment.move_own',requested_clinic,NULL))
 OR EXISTS (SELECT 1 FROM clinic_app.billing_settlement s
 JOIN clinic_app.scheduling_appointment a ON a.id=invoice.appointment_id
 WHERE s.invoice_id=invoice.id AND s.confirmed_by_id=
 NULLIF(current_setting('app.current_user_id',true),'')::uuid
 AND a.status='completed'
 AND clinic_app.billing_collection_authorized(requested_clinic))) THEN
  RAISE EXCEPTION 'billing access denied' USING ERRCODE='42501';
 END IF;
 changed:=terms.completion_entry_id IS NULL;
 IF NOT changed THEN
  IF evidence IS DISTINCT FROM terms.completion_evidence THEN
   RAISE EXCEPTION 'completion evidence conflict' USING ERRCODE='23514';
  END IF;
  entry_id:=terms.completion_entry_id; RETURN NEXT; RETURN;
 END IF;
 IF invoice.state NOT IN ('open','paid')
 OR (invoice.appointment_id IS NOT NULL AND NOT EXISTS (
 SELECT 1 FROM clinic_app.scheduling_appointment a
 WHERE a.id=invoice.appointment_id AND a.clinic_id=requested_clinic
 AND a.status='completed'))
 OR (invoice.appointment_id IS NULL AND evidence IS NULL) THEN
  RAISE EXCEPTION 'verified service completion required' USING ERRCODE='23514',
  CONSTRAINT='billing_completion_required';
 END IF;
 receivable:=CASE terms.payer WHEN 'patient' THEN 'patient_ar' ELSE 'insurer_ar' END;
 lines:=clinic_app.billing_line(receivable,'debit',invoice.amount_minor) ||
  clinic_app.billing_line('service_revenue','credit',invoice.amount_minor) ||
  clinic_app.billing_line('discounts','debit',terms.discount_minor) ||
  clinic_app.billing_line(receivable,'credit',terms.discount_minor) ||
  clinic_app.billing_line('payer_adjustments','debit',terms.payer_adjustment_minor) ||
  clinic_app.billing_line(receivable,'credit',terms.payer_adjustment_minor);
 SELECT coalesce(sum(amount_minor),0) INTO prepaid
 FROM clinic_app.billing_settlementallocation WHERE terms_id=terms.id;
 SELECT coalesce(sum(r.amount_minor),0) INTO refunded
 FROM clinic_app.billing_refund r
 JOIN clinic_app.billing_settlementallocation a ON a.id=r.allocation_id
 WHERE a.terms_id=terms.id AND r.state IN ('approved','sending','confirmed','unknown');
 prepaid:=prepaid-refunded;
 lines:=lines || clinic_app.billing_line('patient_advances','debit',prepaid::bigint)
 || clinic_app.billing_line(receivable,'credit',prepaid::bigint);
 entry_id:=clinic_app.billing_post_internal(book_id,'completion',terms.id,lines);
 UPDATE clinic_app.billing_invoiceterms SET completion_entry_id=entry_id,
 completion_evidence=evidence WHERE id=terms.id;
 RETURN NEXT;
END $f$;

CREATE FUNCTION clinic_app.billing_complete_appointment(
 requested_clinic uuid, requested_appointment uuid)
RETURNS TABLE(entry_id uuid, changed boolean, terms_id uuid, prepared boolean)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE book_id uuid; invoice clinic_app.billing_invoice;
 terms clinic_app.billing_invoiceterms; created boolean;
BEGIN
 IF NOT EXISTS (SELECT 1 FROM clinic_app.scheduling_appointment a
 WHERE a.id=requested_appointment AND a.clinic_id=requested_clinic
 AND a.status='completed' AND a.organization_id=
 NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND (a.practitioner_id=NULLIF(current_setting('app.current_user_id',true),'')::uuid
 AND clinic_app.has_permission('appointment.move_own',requested_clinic,NULL))) THEN
  RAISE EXCEPTION 'billing access denied' USING ERRCODE='42501';
 END IF;
 SELECT id INTO book_id FROM clinic_app.billing_journalbook
 WHERE clinic_id=requested_clinic;
 IF book_id IS NULL THEN RETURN; END IF;
 PERFORM clinic_app.billing_lock_book(book_id);
 FOR invoice IN SELECT i.* FROM clinic_app.billing_invoice i
 WHERE i.clinic_id=requested_clinic AND i.appointment_id=requested_appointment
 AND i.state IN ('open','paid')
 ORDER BY i.id LOOP
  SELECT * INTO terms FROM clinic_app.billing_invoiceterms t
  WHERE t.invoice_id=invoice.id;
  created:=terms.id IS NULL;
  IF created THEN
   INSERT INTO clinic_app.billing_invoiceterms
   (id,organization_id,book_id,invoice_id,payer,discount_minor,payer_adjustment_minor,
    net_minor,created_at)
   VALUES(gen_random_uuid(),invoice.organization_id,book_id,invoice.id,'patient',0,0,
    invoice.amount_minor,statement_timestamp()) RETURNING * INTO terms;
  END IF;
  RETURN QUERY SELECT r.entry_id,r.changed,terms.id,created
   FROM clinic_app.billing_recognize_completion(
   requested_clinic,invoice.id,NULL) r;
 END LOOP;
END $f$;
"""
