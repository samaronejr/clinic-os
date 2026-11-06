"""Frozen charge policies and clinic legal-entity account definitions."""

DEFINITIONS_SQL = """
CREATE FUNCTION clinic_app.billing_definition_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE book clinic_app.billing_journalbook; invoice clinic_app.billing_invoice;
 rule clinic_app.billing_practitionerpayoutrule;
BEGIN
 IF TG_OP<>'INSERT' THEN
  IF TG_TABLE_NAME='billing_invoiceterms' AND TG_OP='UPDATE'
  AND OLD.completion_entry_id IS NULL AND NEW.completion_entry_id IS NOT NULL
  AND to_jsonb(NEW)-'completion_entry_id'-'completion_evidence'
   =to_jsonb(OLD)-'completion_entry_id'-'completion_evidence'
  AND EXISTS (SELECT 1 FROM clinic_app.billing_journalentry e
   WHERE e.id=NEW.completion_entry_id AND e.book_id=NEW.book_id
   AND e.source_kind='completion' AND e.source_id=NEW.id AND e.state='posted')
  THEN RETURN NEW; END IF;
  RAISE EXCEPTION 'financial definitions are immutable' USING ERRCODE='23514';
 END IF;
 IF TG_TABLE_NAME='billing_journalbook' THEN
  IF NEW.currency<>'BRL' OR NEW.policy_version<>'completion-net-v1'
  OR NOT EXISTS (SELECT 1 FROM clinic_app.identity_clinic c
   WHERE c.id=NEW.clinic_id AND c.organization_id=NEW.organization_id)
  OR NEW.organization_id IS DISTINCT FROM
   NULLIF(current_setting('app.current_tenant',true),'')::uuid
  OR NOT clinic_app.has_permission('finance.policy',NEW.clinic_id,NULL) THEN
   RAISE EXCEPTION 'billing access denied' USING ERRCODE='42501';
  END IF;
  RETURN NEW;
 END IF;
 PERFORM clinic_app.billing_lock_book(NEW.book_id);
 SELECT * INTO book FROM clinic_app.billing_journalbook
 WHERE id=NEW.book_id FOR UPDATE;
 IF book.id IS NULL OR book.organization_id<>NEW.organization_id THEN
  RAISE EXCEPTION 'invalid financial scope' USING ERRCODE='23514';
 END IF;
 IF TG_TABLE_NAME='billing_account' THEN
  IF NEW.code NOT IN ('bank','petty_cash','psp_clearing','payout_in_transit',
   'patient_ar','insurer_ar','patient_advances','refunds_payable',
   'unidentified_receipts','insurer_overpayments','practitioner_payables',
   'taxes_payable','service_revenue','contra_revenue','discounts',
   'payer_adjustments','psp_fees','practitioner_compensation','bad_debt',
   'suspense','opening_balances') THEN
   RAISE EXCEPTION 'unknown clinic account' USING ERRCODE='23514';
  END IF;
 ELSIF TG_TABLE_NAME='billing_practitionerpayoutrule' THEN
  IF NOT clinic_app.billing_book_access(book.id,'finance.policy') THEN
   RAISE EXCEPTION 'billing access denied' USING ERRCODE='42501';
  END IF;
  IF NEW.kind NOT IN ('percent','fixed') OR NEW.value<0
  OR (NEW.kind='percent' AND NEW.value>10000)
  OR NEW.policy_version<>book.policy_version
  OR NOT EXISTS (SELECT 1 FROM clinic_app.identity_userclinicrole r
   JOIN clinic_app.identity_user u ON u.id=r.user_id AND u.is_active
   WHERE r.clinic_id=book.clinic_id AND r.user_id=NEW.practitioner_id
   AND r.role IN ('physician','allied_professional'))
  OR (NEW.service_type_id IS NOT NULL AND NOT EXISTS (
   SELECT 1 FROM clinic_app.scheduling_servicetype s
   WHERE s.id=NEW.service_type_id AND s.clinic_id=book.clinic_id)) THEN
   RAISE EXCEPTION 'invalid practitioner policy' USING ERRCODE='23514';
  END IF;
 ELSIF TG_TABLE_NAME='billing_invoiceterms' THEN
  SELECT * INTO invoice FROM clinic_app.billing_invoice WHERE id=NEW.invoice_id;
  IF NOT clinic_app.billing_book_access(book.id,'settlement.post') AND NOT (
   NEW.payout_rule_id IS NULL AND NEW.discount_minor=0
   AND NEW.payer_adjustment_minor=0 AND NEW.payer='patient'
   AND EXISTS (SELECT 1 FROM clinic_app.billing_settlement s
   WHERE s.invoice_id=invoice.id AND s.confirmed_by_id=
   NULLIF(current_setting('app.current_user_id',true),'')::uuid
   AND clinic_app.billing_charge_authorized(book.clinic_id))) AND NOT (
   NEW.payout_rule_id IS NULL AND NEW.discount_minor=0
   AND NEW.payer_adjustment_minor=0 AND NEW.payer='patient'
   AND EXISTS (SELECT 1 FROM clinic_app.scheduling_appointment a
   WHERE a.id=invoice.appointment_id AND a.status='completed'
   AND a.practitioner_id=NULLIF(current_setting('app.current_user_id',true),'')::uuid
   AND clinic_app.has_permission('appointment.move_own',book.clinic_id,NULL))) THEN
   RAISE EXCEPTION 'billing access denied' USING ERRCODE='42501';
  END IF;
  IF invoice.id IS NULL OR invoice.clinic_id<>book.clinic_id
  OR invoice.organization_id<>book.organization_id
  OR invoice.state NOT IN ('open','paid')
  OR NEW.discount_minor<0 OR NEW.payer_adjustment_minor<0
  OR NEW.discount_minor::numeric+NEW.payer_adjustment_minor>=invoice.amount_minor
  OR NEW.net_minor::numeric<>invoice.amount_minor::numeric
    -NEW.discount_minor-NEW.payer_adjustment_minor
  OR NEW.payer NOT IN ('patient','insurer')
  OR NEW.completion_entry_id IS NOT NULL OR NEW.completion_evidence IS NOT NULL THEN
   RAISE EXCEPTION 'invalid financial terms' USING ERRCODE='23514';
  END IF;
  IF NEW.payout_rule_id IS NOT NULL THEN
   SELECT * INTO rule FROM clinic_app.billing_practitionerpayoutrule
   WHERE id=NEW.payout_rule_id;
   IF rule.id IS NULL OR rule.book_id<>book.id
   OR (rule.kind='fixed' AND rule.value>NEW.net_minor)
   OR (invoice.appointment_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM clinic_app.scheduling_appointment a
    WHERE a.id=invoice.appointment_id AND a.practitioner_id=rule.practitioner_id
    AND (rule.service_type_id IS NULL OR a.service_type_id=rule.service_type_id)))
   THEN RAISE EXCEPTION 'invalid financial rule binding' USING ERRCODE='23514';
   END IF;
  END IF;
 END IF;
 RETURN NEW;
END $f$;

CREATE FUNCTION clinic_app.billing_seed_chart()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE code text;
BEGIN
 FOREACH code IN ARRAY ARRAY['bank','petty_cash','psp_clearing','payout_in_transit',
 'patient_ar','insurer_ar','patient_advances','refunds_payable',
 'unidentified_receipts','insurer_overpayments','practitioner_payables',
 'taxes_payable','service_revenue','contra_revenue','discounts',
 'payer_adjustments','psp_fees','practitioner_compensation','bad_debt',
 'suspense','opening_balances'] LOOP
  INSERT INTO clinic_app.billing_account(id,organization_id,book_id,code)
  VALUES(gen_random_uuid(),NEW.organization_id,NEW.id,code);
 END LOOP;
 RETURN NEW;
END $f$;
"""
