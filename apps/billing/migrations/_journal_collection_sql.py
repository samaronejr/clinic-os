"""Verified receipts and exact net collection entitlement."""

COLLECTION_SQL = """
CREATE FUNCTION clinic_app.billing_refunded_prepayments(requested_terms uuid)
RETURNS numeric LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT coalesce(sum(r.amount_minor),0) FROM clinic_app.billing_refund r
 JOIN clinic_app.billing_settlementallocation a ON a.id=r.allocation_id
 WHERE a.terms_id=requested_terms AND r.state='confirmed'
 AND EXISTS (SELECT 1 FROM clinic_app.billing_journalline l
 JOIN clinic_app.billing_account c ON c.id=l.account_id
 WHERE l.entry_id=r.approval_entry_id AND c.code='patient_advances' AND l.side='debit')
$f$;

CREATE FUNCTION clinic_app.billing_settlement_journal_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE book clinic_app.billing_journalbook; liability text; legacy boolean;
BEGIN
 IF TG_OP='DELETE' THEN
  RAISE EXCEPTION 'settlement history is immutable' USING ERRCODE='23514';
 END IF;
 PERFORM clinic_app.billing_lock_book(NEW.book_id);
 SELECT * INTO book FROM clinic_app.billing_journalbook WHERE id=NEW.book_id;
 legacy := NEW.legacy_settlement_id IS NOT NULL AND EXISTS (
 SELECT 1 FROM clinic_app.billing_settlement s
 JOIN clinic_app.billing_invoice i ON i.id=s.invoice_id
 WHERE s.id=NEW.legacy_settlement_id AND i.clinic_id=book.clinic_id
 AND s.organization_id=book.organization_id AND s.amount_minor=NEW.amount_minor
 AND s.confirmation_reference=NEW.evidence_reference
 AND s.confirmed_by_id=NULLIF(current_setting('app.current_user_id',true),'')::uuid
 AND clinic_app.billing_collection_authorized(book.clinic_id));
 IF book.organization_id IS DISTINCT FROM NEW.organization_id
 OR NEW.amount_minor<=0 OR NEW.kind NOT IN ('patient','insurer','unidentified')
 OR NEW.account NOT IN ('bank','petty_cash','psp_clearing')
 OR (NEW.legacy_settlement_id IS NOT NULL AND NOT legacy) THEN
  RAISE EXCEPTION 'invalid settlement binding' USING ERRCODE='23514';
 END IF;
 IF TG_OP='INSERT' THEN
  IF NOT (legacy OR clinic_app.billing_book_access(book.id,'charge.collect'))
  OR NEW.state<>'observed' OR NEW.entry_id IS NOT NULL
  OR NEW.verified_by_id IS NOT NULL OR NEW.verified_at IS NOT NULL THEN
   RAISE EXCEPTION 'billing access denied' USING ERRCODE='42501';
  END IF;
  NEW.created_at:=statement_timestamp();
 ELSE
  IF to_jsonb(NEW)-'state'<>to_jsonb(OLD)-'state'
  OR NOT (legacy OR clinic_app.billing_book_access(book.id,'settlement.post'))
  OR NOT ((OLD.state='observed' AND NEW.state IN ('verified','quarantined'))
   OR (OLD.state='verified' AND NEW.state IN ('posted','quarantined'))
   OR (OLD.state='quarantined' AND NEW.state='verified')) THEN
   RAISE EXCEPTION 'invalid settlement transition' USING ERRCODE='23514';
  END IF;
  IF NEW.state='verified' THEN
   NEW.verified_by_id:=
    NULLIF(current_setting('app.current_user_id',true),'')::uuid;
   NEW.verified_at:=statement_timestamp();
  ELSIF NEW.state='posted' THEN
   liability:=CASE NEW.kind WHEN 'patient' THEN 'patient_advances'
    WHEN 'insurer' THEN 'insurer_overpayments' ELSE 'unidentified_receipts' END;
   NEW.entry_id:=clinic_app.billing_post_internal(book.id,'collection',NEW.id,
    clinic_app.billing_line(NEW.account,'debit',NEW.amount_minor) ||
    clinic_app.billing_line(liability,'credit',NEW.amount_minor));
  END IF;
 END IF;
 RETURN NEW;
END $f$;

CREATE FUNCTION clinic_app.billing_allocation_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE settlement clinic_app.billing_journalsettlement;
 terms clinic_app.billing_invoiceterms; rule clinic_app.billing_practitionerpayoutrule;
 used numeric; collected numeric; earned numeric; returned numeric; reversed numeric;
 delta numeric; liability text; target text; reversed_entry uuid;
 lines jsonb; trusted_legacy boolean;
BEGIN
 IF TG_OP<>'INSERT' THEN
  RAISE EXCEPTION 'allocation history is immutable' USING ERRCODE='23514';
 END IF;
 PERFORM clinic_app.billing_lock_book(NEW.book_id);
 SELECT * INTO settlement FROM clinic_app.billing_journalsettlement
 WHERE id=NEW.settlement_id;
 SELECT * INTO terms FROM clinic_app.billing_invoiceterms WHERE id=NEW.terms_id;
 trusted_legacy:=settlement.legacy_settlement_id IS NOT NULL AND EXISTS (
 SELECT 1 FROM clinic_app.billing_settlement s
 WHERE s.id=settlement.legacy_settlement_id
 AND s.invoice_id=terms.invoice_id AND s.confirmed_by_id=
 NULLIF(current_setting('app.current_user_id',true),'')::uuid);
 IF settlement.id IS NULL OR terms.id IS NULL
 OR settlement.book_id<>NEW.book_id OR terms.book_id<>NEW.book_id
 OR terms.organization_id<>NEW.organization_id
 OR settlement.organization_id<>NEW.organization_id
 OR settlement.state<>'posted' OR NEW.amount_minor<=0
 OR NEW.repasse_minor<>0 OR NEW.repasse_side<>'credit' OR NEW.entry_id IS NOT NULL
 OR NOT (trusted_legacy OR
 clinic_app.billing_book_access(NEW.book_id,'settlement.post'))
 OR (settlement.legacy_settlement_id IS NOT NULL AND NOT EXISTS (
 SELECT 1 FROM clinic_app.billing_settlement s
 WHERE s.id=settlement.legacy_settlement_id
 AND s.invoice_id=terms.invoice_id)) THEN
  RAISE EXCEPTION 'invalid settlement allocation' USING ERRCODE='23514';
 END IF;
 IF (NEW.group_id IS NULL)<>(NEW.group_fingerprint IS NULL)
 OR (NEW.group_fingerprint IS NOT NULL AND octet_length(NEW.group_fingerprint)<>32)
 OR EXISTS (SELECT 1 FROM clinic_app.billing_settlementallocation a
 WHERE a.book_id=NEW.book_id AND a.group_id=NEW.group_id
 AND (a.group_fingerprint<>NEW.group_fingerprint OR a.settlement_id<>NEW.settlement_id))
 THEN
  RAISE EXCEPTION 'allocation group conflict' USING ERRCODE='23514';
 END IF;
 SELECT coalesce(sum(amount_minor),0) INTO used
 FROM clinic_app.billing_settlementallocation WHERE settlement_id=settlement.id;
 used:=used+(SELECT coalesce(sum(amount_minor),0) FROM clinic_app.billing_refund
 WHERE settlement_id=settlement.id AND allocation_id IS NULL AND state<>'rejected');
 SELECT coalesce(sum(amount_minor),0),coalesce(sum(
 CASE repasse_side WHEN 'credit' THEN repasse_minor::numeric
 ELSE -repasse_minor::numeric END),0)
 INTO collected,earned FROM clinic_app.billing_settlementallocation
 WHERE terms_id=terms.id;
 IF used+NEW.amount_minor>settlement.amount_minor
 OR collected-clinic_app.billing_refunded_prepayments(terms.id)
  +NEW.amount_minor>terms.net_minor THEN
  RAISE EXCEPTION 'allocation exceeds available amount' USING ERRCODE='23514',
  CONSTRAINT='billing_allocation_capacity';
 END IF;
 IF terms.payout_rule_id IS NOT NULL THEN
  SELECT * INTO rule FROM clinic_app.billing_practitionerpayoutrule
  WHERE id=terms.payout_rule_id;
  SELECT coalesce(sum(r.amount_minor),0),coalesce(sum(
  CASE a.repasse_side WHEN 'credit' THEN r.repasse_reversed_minor::numeric
  ELSE -r.repasse_reversed_minor::numeric END),0)
  INTO returned,reversed FROM clinic_app.billing_refund r
  JOIN clinic_app.billing_settlementallocation a ON a.id=r.allocation_id
  WHERE a.terms_id=terms.id AND r.state='confirmed';
  delta:=clinic_app.billing_round_ratio(
   (collected-returned+NEW.amount_minor)*rule.value,
   CASE rule.kind WHEN 'percent' THEN 10000 ELSE terms.net_minor END)-(earned-reversed);
  NEW.repasse_minor:=abs(delta)::bigint;
  NEW.repasse_side:=CASE WHEN delta<0 THEN 'debit' ELSE 'credit' END;
  IF delta<0 THEN
   SELECT entry_id INTO reversed_entry FROM clinic_app.billing_settlementallocation
   WHERE terms_id=terms.id AND repasse_side='credit' AND repasse_minor>0
   ORDER BY created_at,id LIMIT 1;
  END IF;
 END IF;
 liability:=CASE settlement.kind WHEN 'patient' THEN 'patient_advances'
 WHEN 'insurer' THEN 'insurer_overpayments' ELSE 'unidentified_receipts' END;
 target:=CASE WHEN terms.completion_entry_id IS NULL THEN 'patient_advances'
 WHEN terms.payer='patient' THEN 'patient_ar' ELSE 'insurer_ar' END;
 lines:=clinic_app.billing_line(liability,'debit',NEW.amount_minor) ||
 clinic_app.billing_line(target,'credit',NEW.amount_minor);
 IF NEW.repasse_minor>0 THEN
  lines:=lines ||
   clinic_app.billing_line('practitioner_compensation',
    CASE NEW.repasse_side WHEN 'credit' THEN 'debit' ELSE 'credit' END,
    NEW.repasse_minor,
    rule.practitioner_id) ||
   clinic_app.billing_line('practitioner_payables',NEW.repasse_side,NEW.repasse_minor,
    rule.practitioner_id);
 END IF;
 NEW.entry_id:=clinic_app.billing_post_internal(
 NEW.book_id,'allocation',NEW.id,lines,reversed_entry);
 NEW.created_at:=statement_timestamp();
 RETURN NEW;
END $f$;
"""
