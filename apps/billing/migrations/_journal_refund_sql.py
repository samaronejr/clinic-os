"""Reservation before dispatch and linked proportional repasse reversals."""

REFUND_SQL = """
CREATE FUNCTION clinic_app.billing_refund_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE book clinic_app.billing_journalbook;
 allocation clinic_app.billing_settlementallocation;
 settlement clinic_app.billing_journalsettlement; terms clinic_app.billing_invoiceterms;
 rule clinic_app.billing_practitionerpayoutrule;
 reserved numeric; used numeric; reversed numeric; refunded numeric; principal text;
 lines jsonb; ignored uuid; actor uuid;
BEGIN
 IF TG_OP='DELETE' THEN
  RAISE EXCEPTION 'refund history is immutable' USING ERRCODE='23514';
 END IF;
 PERFORM clinic_app.billing_lock_book(NEW.book_id);
 SELECT * INTO book FROM clinic_app.billing_journalbook WHERE id=NEW.book_id;
 SELECT * INTO settlement FROM clinic_app.billing_journalsettlement
 WHERE id=NEW.settlement_id;
 SELECT * INTO allocation FROM clinic_app.billing_settlementallocation
 WHERE id=NEW.allocation_id;
 SELECT * INTO terms FROM clinic_app.billing_invoiceterms WHERE id=allocation.terms_id;
 actor:=NULLIF(current_setting('app.current_user_id',true),'')::uuid;
 IF NEW.organization_id IS DISTINCT FROM book.organization_id
 OR settlement.id IS NULL OR settlement.book_id<>book.id OR settlement.state<>'posted'
 OR NEW.amount_minor<=0 OR NEW.kind NOT IN ('refund','chargeback')
 OR (NEW.allocation_id IS NOT NULL AND (allocation.id IS NULL
 OR allocation.book_id<>book.id OR allocation.settlement_id<>settlement.id)) THEN
  RAISE EXCEPTION 'invalid refund binding' USING ERRCODE='23514';
 END IF;
 IF TG_OP='INSERT' THEN
  IF NOT (clinic_app.billing_book_access(book.id,'refund.request')
   OR clinic_app.billing_book_access(book.id,'refund.approve'))
  OR NEW.state<>'requested' OR NEW.requested_by_id IS DISTINCT FROM actor
  OR NEW.approved_by_id IS NOT NULL OR NEW.approval_session_id IS NOT NULL
  OR NEW.approval_entry_id IS NOT NULL OR NEW.repasse_entry_id IS NOT NULL
  OR NEW.repasse_reversed_minor<>0 OR NEW.evidence_reference IS NOT NULL THEN
   RAISE EXCEPTION 'billing access denied' USING ERRCODE='42501';
  END IF;
  SELECT coalesce(sum(amount_minor),0) INTO reserved
  FROM clinic_app.billing_refund
  WHERE settlement_id=settlement.id AND state<>'rejected';
  IF reserved+NEW.amount_minor>settlement.amount_minor THEN
   RAISE EXCEPTION 'refund exceeds collection' USING ERRCODE='23514',
   CONSTRAINT='billing_refund_capacity';
  END IF;
  IF allocation.id IS NOT NULL THEN
   SELECT coalesce(sum(amount_minor),0) INTO reserved
   FROM clinic_app.billing_refund
   WHERE allocation_id=allocation.id AND state<>'rejected';
   IF reserved+NEW.amount_minor>allocation.amount_minor THEN
    RAISE EXCEPTION 'refund exceeds allocation' USING ERRCODE='23514',
    CONSTRAINT='billing_refund_capacity';
   END IF;
  ELSE
   SELECT coalesce(sum(amount_minor),0) INTO used
   FROM clinic_app.billing_settlementallocation WHERE settlement_id=settlement.id;
   SELECT coalesce(sum(amount_minor),0) INTO reserved
   FROM clinic_app.billing_refund WHERE settlement_id=settlement.id
   AND allocation_id IS NULL AND state<>'rejected';
   IF used+reserved+NEW.amount_minor>settlement.amount_minor THEN
    RAISE EXCEPTION 'refund exceeds unallocated money' USING ERRCODE='23514',
    CONSTRAINT='billing_refund_capacity';
   END IF;
  END IF;
  NEW.created_at:=statement_timestamp();
  RETURN NEW;
 END IF;
 IF to_jsonb(NEW)-'state'-'evidence_reference'
  <>to_jsonb(OLD)-'state'-'evidence_reference'
 OR NOT clinic_app.billing_book_access(book.id,'refund.approve')
 OR NOT ((OLD.state='requested' AND NEW.state='approved')
  OR (OLD.state='approved' AND NEW.state='sending')
  OR (OLD.state='sending' AND NEW.state IN ('confirmed','rejected','unknown'))
  OR (OLD.state='unknown' AND NEW.state IN ('confirmed','rejected'))) THEN
  RAISE EXCEPTION 'invalid refund transition' USING ERRCODE='23514';
 END IF;
 PERFORM clinic_app.billing_finance_session(book.clinic_id);
 IF OLD.evidence_reference IS NOT NULL
 OR (NEW.state IN ('confirmed','rejected') AND NEW.evidence_reference IS NULL)
 OR (NEW.state NOT IN ('confirmed','rejected')
 AND NEW.evidence_reference IS NOT NULL) THEN
  RAISE EXCEPTION 'authoritative outcome evidence required' USING ERRCODE='23514';
 END IF;
 IF NEW.state='approved' THEN
  NEW.approved_by_id:=actor;
  NEW.approval_session_id:=clinic_app.billing_finance_session(book.clinic_id);
  principal:=CASE WHEN allocation.id IS NOT NULL
   THEN CASE WHEN terms.completion_entry_id IS NULL THEN 'patient_advances'
    ELSE 'contra_revenue' END
   ELSE CASE settlement.kind WHEN 'patient' THEN 'patient_advances'
    WHEN 'insurer' THEN 'insurer_overpayments' ELSE 'unidentified_receipts' END END;
  NEW.approval_entry_id:=clinic_app.billing_post_internal(book.id,'refund_approved',
   NEW.id,clinic_app.billing_line(principal,'debit',NEW.amount_minor) ||
   clinic_app.billing_line('refunds_payable','credit',NEW.amount_minor),
   coalesce(allocation.entry_id,settlement.entry_id));
 ELSE
  IF NEW.state='confirmed' THEN
   ignored:=clinic_app.billing_post_internal(book.id,'refund_confirmed',NEW.id,
    clinic_app.billing_line('refunds_payable','debit',NEW.amount_minor) ||
    clinic_app.billing_line(settlement.account,'credit',NEW.amount_minor));
   IF allocation.repasse_minor>0 THEN
    SELECT coalesce(sum(amount_minor),0),coalesce(sum(repasse_reversed_minor),0)
    INTO refunded,reversed FROM clinic_app.billing_refund
    WHERE allocation_id=allocation.id AND state='confirmed';
    NEW.repasse_reversed_minor:=clinic_app.billing_round_ratio(
     (refunded+NEW.amount_minor)*allocation.repasse_minor,
     allocation.amount_minor)-reversed;
    SELECT * INTO rule FROM clinic_app.billing_practitionerpayoutrule
    WHERE id=terms.payout_rule_id;
    IF NEW.repasse_reversed_minor>0 THEN
     NEW.repasse_entry_id:=clinic_app.billing_post_internal(book.id,'repasse_reversal',
      NEW.id,clinic_app.billing_line('practitioner_payables',
       CASE allocation.repasse_side WHEN 'credit' THEN 'debit' ELSE 'credit' END,
       NEW.repasse_reversed_minor,rule.practitioner_id) ||
      clinic_app.billing_line('practitioner_compensation',
       CASE allocation.repasse_side WHEN 'credit' THEN 'credit' ELSE 'debit' END,
       NEW.repasse_reversed_minor,rule.practitioner_id),allocation.entry_id);
    END IF;
   END IF;
  ELSIF NEW.state='rejected' THEN
   SELECT jsonb_agg(jsonb_build_object('account',a.code,
    'side',CASE l.side WHEN 'debit' THEN 'credit' ELSE 'debit' END,
    'amount_minor',l.amount_minor::text,'practitioner_id',l.practitioner_id,
    'owner_id',l.owner_id,'due_on',l.due_on)) INTO lines
   FROM clinic_app.billing_journalline l
   JOIN clinic_app.billing_account a ON a.id=l.account_id
   WHERE l.entry_id=OLD.approval_entry_id;
   IF terms.completion_entry_id IS NOT NULL AND EXISTS (
    SELECT 1 FROM clinic_app.billing_journalline l
    JOIN clinic_app.billing_account a ON a.id=l.account_id
    WHERE l.entry_id=OLD.approval_entry_id AND a.code='patient_advances'
    AND l.side='debit') THEN
    lines:=lines || clinic_app.billing_line('patient_advances','debit',NEW.amount_minor)
    || clinic_app.billing_line(CASE terms.payer WHEN 'patient' THEN 'patient_ar'
     ELSE 'insurer_ar' END,'credit',NEW.amount_minor);
   END IF;
   ignored:=clinic_app.billing_post_internal(book.id,'refund_rejected',NEW.id,
    lines,OLD.approval_entry_id);
  END IF;
 END IF;
 RETURN NEW;
END $f$;
"""
