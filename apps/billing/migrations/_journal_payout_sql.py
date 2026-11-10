"""Reserve earned practitioner debt and retain unknown outcomes in transit."""

PAYOUT_SQL = """
CREATE FUNCTION clinic_app.billing_payout_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE book clinic_app.billing_journalbook; payable numeric; reserved numeric;
 actor uuid; ignored uuid;
BEGIN
 IF TG_OP='DELETE' THEN
  RAISE EXCEPTION 'payout history is immutable' USING ERRCODE='23514';
 END IF;
 PERFORM clinic_app.billing_lock_book(NEW.book_id);
 SELECT * INTO book FROM clinic_app.billing_journalbook WHERE id=NEW.book_id;
 actor:=NULLIF(current_setting('app.current_user_id',true),'')::uuid;
 IF book.organization_id IS DISTINCT FROM NEW.organization_id
 OR NEW.amount_minor<=0 THEN
  RAISE EXCEPTION 'invalid payout scope' USING ERRCODE='23514';
 END IF;
 IF TG_OP='INSERT' THEN
  IF NOT (clinic_app.billing_book_access(book.id,'payout.request')
   OR clinic_app.billing_book_access(book.id,'payout.approve'))
  OR NEW.state<>'requested' OR NEW.requested_by_id IS DISTINCT FROM actor
  OR NEW.approved_by_id IS NOT NULL OR NEW.approval_session_id IS NOT NULL
  OR NEW.evidence_reference IS NOT NULL THEN
   RAISE EXCEPTION 'billing access denied' USING ERRCODE='42501';
  END IF;
  SELECT coalesce(sum(CASE l.side WHEN 'credit' THEN l.amount_minor::numeric
   ELSE -l.amount_minor::numeric END),0) INTO payable
  FROM clinic_app.billing_journalline l
  JOIN clinic_app.billing_account a ON a.id=l.account_id
  JOIN clinic_app.billing_journalentry e ON e.id=l.entry_id
  WHERE a.book_id=book.id AND a.code='practitioner_payables'
  AND l.practitioner_id=NEW.practitioner_id AND e.state='posted';
  SELECT coalesce(sum(amount_minor),0) INTO reserved FROM clinic_app.billing_payout
  WHERE book_id=book.id AND practitioner_id=NEW.practitioner_id AND state='requested';
  IF NEW.amount_minor+reserved>payable THEN
   RAISE EXCEPTION 'payout exceeds available payable' USING ERRCODE='23514',
   CONSTRAINT='billing_payout_capacity';
  END IF;
  NEW.created_at:=statement_timestamp();
  RETURN NEW;
 END IF;
 IF to_jsonb(NEW)-'state'-'evidence_reference'
 <>to_jsonb(OLD)-'state'-'evidence_reference'
 OR NOT clinic_app.billing_book_access(book.id,'payout.approve')
 OR NOT ((OLD.state='requested' AND NEW.state='approved')
  OR (OLD.state='approved' AND NEW.state='sending')
  OR (OLD.state='sending' AND NEW.state IN ('confirmed','unknown'))
  OR (OLD.state='unknown' AND NEW.state='confirmed')) THEN
  RAISE EXCEPTION 'invalid payout transition' USING ERRCODE='23514';
 END IF;
 PERFORM clinic_app.billing_finance_session(book.clinic_id);
 IF OLD.evidence_reference IS NOT NULL
 OR (NEW.state='confirmed' AND NEW.evidence_reference IS NULL)
 OR (NEW.state<>'confirmed' AND NEW.evidence_reference IS NOT NULL) THEN
  RAISE EXCEPTION 'authoritative outcome evidence required' USING ERRCODE='23514';
 END IF;
 IF NEW.state='approved' THEN
  -- A refund since request may have reduced the entitlement. Recheck all claims.
  SELECT coalesce(sum(CASE l.side WHEN 'credit' THEN l.amount_minor::numeric
   ELSE -l.amount_minor::numeric END),0) INTO payable
  FROM clinic_app.billing_journalline l
  JOIN clinic_app.billing_account a ON a.id=l.account_id
  JOIN clinic_app.billing_journalentry e ON e.id=l.entry_id
  WHERE a.book_id=book.id AND a.code='practitioner_payables'
  AND l.practitioner_id=NEW.practitioner_id AND e.state='posted';
  SELECT coalesce(sum(amount_minor),0) INTO reserved FROM clinic_app.billing_payout
  WHERE book_id=book.id AND practitioner_id=NEW.practitioner_id AND state='requested';
  IF reserved>payable THEN
   RAISE EXCEPTION 'payout entitlement changed' USING ERRCODE='23514',
   CONSTRAINT='billing_payout_capacity';
  END IF;
  NEW.approved_by_id:=actor;
  NEW.approval_session_id:=clinic_app.billing_finance_session(book.clinic_id);
  ignored:=clinic_app.billing_post_internal(book.id,'payout_approved',NEW.id,
   clinic_app.billing_line('practitioner_payables','debit',NEW.amount_minor,
    NEW.practitioner_id) ||
   clinic_app.billing_line('payout_in_transit','credit',NEW.amount_minor));
 ELSE
  IF NEW.state='confirmed' THEN
   ignored:=clinic_app.billing_post_internal(book.id,'payout_confirmed',NEW.id,
    clinic_app.billing_line('payout_in_transit','debit',NEW.amount_minor) ||
    clinic_app.billing_line('bank','credit',NEW.amount_minor));
  END IF;
 END IF;
 RETURN NEW;
END $f$;
"""
