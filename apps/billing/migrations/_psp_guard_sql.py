"""Enforce immutable requests, exact economic scope and bounded PSP evidence."""

SQL = """
CREATE FUNCTION clinic_app.billing_psp_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE book clinic_app.billing_journalbook; intent clinic_app.billing_pspintent;
 policy clinic_app.billing_psppolicy; terms clinic_app.billing_invoiceterms;
 version clinic_app.providers_capabilityversion; current_id uuid; actor uuid;
 refund clinic_app.billing_refund; payout clinic_app.billing_payout;
 original clinic_app.billing_pspintent; reference text; permission text;
BEGIN
 IF TG_OP<>'INSERT' THEN
  RAISE EXCEPTION 'PSP history is immutable' USING ERRCODE='23514';
 END IF;
 actor:=NULLIF(current_setting('app.current_user_id',true),'')::uuid;
 IF TG_TABLE_NAME IN ('billing_psppolicy','billing_pspintent') THEN
  PERFORM clinic_app.billing_lock_book(NEW.book_id);
  SELECT * INTO book FROM clinic_app.billing_journalbook WHERE id=NEW.book_id;
  IF NEW.organization_id IS DISTINCT FROM book.organization_id THEN
   RAISE EXCEPTION 'invalid PSP scope' USING ERRCODE='23514';
  END IF;
  IF TG_TABLE_NAME='billing_psppolicy' THEN
   IF NOT clinic_app.billing_book_access(book.id,'finance.policy')
   OR NEW.max_installments NOT BETWEEN 1 AND 12 THEN
    RAISE EXCEPTION 'billing access denied' USING ERRCODE='42501';
   END IF;
  ELSE
   SELECT * INTO policy FROM clinic_app.billing_psppolicy WHERE id=NEW.policy_id;
   SELECT * INTO version FROM clinic_app.providers_capabilityversion
    WHERE id=NEW.capability_version_id;
   SELECT current_version_id INTO current_id
    FROM clinic_app.providers_providercapability
    WHERE key=CASE NEW.method WHEN 'pix' THEN 'pix' ELSE 'psp_card' END
    AND (clinic_id=book.clinic_id OR clinic_id IS NULL)
    ORDER BY clinic_id NULLS LAST LIMIT 1;
   permission:=CASE NEW.kind WHEN 'charge' THEN 'charge.collect'
    WHEN 'refund' THEN 'refund.approve' WHEN 'payout' THEN 'payout.approve' END;
   IF permission IS NULL
   OR NOT clinic_app.billing_book_access(book.id,permission)
   OR NEW.requested_by_id IS DISTINCT FROM actor
   OR NEW.provider<>'recorded-psp-v1' OR NEW.amount_minor<=0 OR NEW.currency<>'BRL'
   OR NEW.method NOT IN ('pix','card') OR NEW.expires_at<=statement_timestamp()
   OR NEW.installments NOT BETWEEN 1 AND policy.max_installments
   OR (NEW.method='pix' AND NEW.installments<>1)
   OR policy.id IS NULL OR policy.book_id<>book.id
   OR policy.id IS DISTINCT FROM (SELECT p.id FROM clinic_app.billing_psppolicy p
    WHERE p.book_id=book.id ORDER BY p.created_at DESC,p.id DESC LIMIT 1)
   OR version.id IS NULL OR version.id IS DISTINCT FROM current_id
   OR version.state<>'sandbox' OR version.approval_id IS NULL
   OR version.provider<>NEW.provider OR version.environment<>'sandbox'
   OR version.account<>'fixture' THEN
    RAISE EXCEPTION 'invalid PSP request binding' USING ERRCODE='23514';
   END IF;
   IF NEW.kind='charge' THEN
    SELECT * INTO terms FROM clinic_app.billing_invoiceterms
     WHERE invoice_id=NEW.target_id AND book_id=book.id;
    IF terms.id IS NULL OR terms.payer<>'patient' OR terms.net_minor<>NEW.amount_minor
    OR NEW.original_id IS NOT NULL OR NEW.original_reference<>''
    OR NEW.payee_reference<>''
    OR NEW.dispatch_session_id IS NOT NULL
    OR EXISTS (SELECT 1 FROM clinic_app.billing_settlementallocation
     WHERE terms_id=terms.id)
    OR NOT EXISTS (SELECT 1 FROM clinic_app.billing_invoice i
     WHERE i.id=terms.invoice_id AND i.state='open') THEN
     RAISE EXCEPTION 'invalid PSP invoice binding' USING ERRCODE='23514';
    END IF;
   ELSE
    IF NEW.dispatch_session_id IS DISTINCT FROM
     clinic_app.billing_finance_session(book.clinic_id) OR NEW.installments<>1 THEN
     RAISE EXCEPTION 'invalid PSP outgoing installments' USING ERRCODE='23514';
    END IF;
    IF NEW.kind='refund' THEN
     SELECT * INTO refund FROM clinic_app.billing_refund WHERE id=NEW.target_id;
     SELECT * INTO original FROM clinic_app.billing_pspintent WHERE id=NEW.original_id;
     SELECT r.provider_reference INTO reference FROM clinic_app.billing_pspbinding r
      WHERE r.intent_id=original.id;
     IF refund.id IS NULL OR refund.book_id<>book.id OR refund.state<>'approved'
     OR refund.kind<>'refund' OR refund.amount_minor<>NEW.amount_minor
     OR original.id IS NULL OR original.book_id<>book.id OR original.kind<>'charge'
     OR original.method<>NEW.method OR reference IS NULL
     OR NEW.original_reference<>reference OR NEW.payee_reference<>''
     OR NOT EXISTS (SELECT 1 FROM clinic_app.billing_journalsettlement s
      WHERE s.id=refund.settlement_id AND s.evidence_reference=original.id) THEN
      RAISE EXCEPTION 'invalid PSP refund binding' USING ERRCODE='23514';
     END IF;
    ELSE
     SELECT * INTO payout FROM clinic_app.billing_payout WHERE id=NEW.target_id;
     IF payout.id IS NULL OR payout.book_id<>book.id OR payout.state<>'approved'
     OR payout.amount_minor<>NEW.amount_minor OR NEW.original_id IS NOT NULL
     OR NEW.original_reference<>'' OR NEW.method<>'pix'
     OR NEW.payee_reference<>replace(payout.practitioner_id::text,'-','') THEN
      RAISE EXCEPTION 'invalid PSP payout binding' USING ERRCODE='23514';
     END IF;
    END IF;
   END IF;
  END IF;
 ELSIF TG_TABLE_NAME='billing_pspreceipt' THEN
  SELECT * INTO intent FROM clinic_app.billing_pspintent WHERE id=NEW.intent_id;
  PERFORM clinic_app.billing_lock_book(intent.book_id);
  IF intent.id IS NULL OR intent.kind<>'charge'
  OR NEW.organization_id IS DISTINCT FROM intent.organization_id
  OR NEW.amount_minor<>intent.amount_minor
  OR NOT clinic_app.billing_book_access(intent.book_id,'settlement.post')
  OR NOT EXISTS (
   SELECT 1 FROM clinic_app.billing_journalsettlement s
   JOIN clinic_app.billing_settlementallocation a ON a.settlement_id=s.id
   JOIN clinic_app.billing_invoiceterms t ON t.id=a.terms_id
   WHERE s.id=NEW.settlement_id AND a.id=NEW.allocation_id
   AND s.book_id=intent.book_id AND s.state='posted'
   AND s.evidence_reference=intent.id AND s.amount_minor=NEW.amount_minor
   AND a.amount_minor=NEW.amount_minor AND t.invoice_id=intent.target_id) THEN
   RAISE EXCEPTION 'invalid PSP receipt binding' USING ERRCODE='23514';
  END IF;
  NEW.issued_at:=clock_timestamp();
  RETURN NEW;
 ELSE
  SELECT * INTO intent FROM clinic_app.billing_pspintent WHERE id=NEW.intent_id;
  SELECT * INTO book FROM clinic_app.billing_journalbook WHERE id=intent.book_id;
  PERFORM pg_advisory_xact_lock(hashtextextended(
   'clinic-lock-v1:clinic:'||book.clinic_id::text,0));
  PERFORM pg_advisory_xact_lock(hashtextextended(
   'clinic-audit:'||book.organization_id::text,0));
  PERFORM pg_advisory_xact_lock(hashtextextended(
   'clinic-lock-v1:psp:'||intent.id::text,0));
  IF intent.id IS NULL OR NEW.organization_id IS DISTINCT FROM intent.organization_id
  OR NEW.provider<>intent.provider
  OR intent.requested_by_id IS DISTINCT FROM actor
  OR NOT clinic_app.billing_psp_access(intent.id) THEN
   RAISE EXCEPTION 'billing access denied' USING ERRCODE='42501';
  END IF;
  IF TG_TABLE_NAME='billing_pspbinding' THEN
   IF NEW.provider_reference !~ '^[A-Za-z0-9_-]{1,128}$' THEN
    RAISE EXCEPTION 'invalid PSP reference' USING ERRCODE='23514';
   END IF;
  ELSE
   IF NEW.event_id !~ '^[A-Za-z0-9_-]{1,128}$'
   OR NEW.source NOT IN ('callback','status','statement')
   OR NEW.decision NOT IN
    ('recorded','verified','matched','quarantined','superseded','unknown')
   OR NEW.status NOT IN
    ('pending','settled','confirmed','rejected','expired','reversed','unknown')
   OR NEW.currency !~ '^[A-Z]{3}$' OR NEW.amount_minor<=0
   OR NEW.fee_minor>NEW.amount_minor
   OR (NEW.decision IN ('verified','matched') AND
    (NEW.amount_minor<>intent.amount_minor OR NEW.currency<>intent.currency
     OR NEW.version=0 OR NEW.status<>
      CASE intent.kind WHEN 'charge' THEN 'settled' ELSE 'confirmed' END
     OR NOT EXISTS (SELECT 1 FROM clinic_app.billing_pspbinding
      WHERE intent_id=intent.id)))
   OR (NEW.decision='matched' AND NEW.source<>'statement')
   OR coalesce(octet_length(NEW.payment_code),0)>8192
   OR coalesce(octet_length(NEW.payment_url),0)>8192 THEN
    RAISE EXCEPTION 'invalid PSP evidence' USING ERRCODE='23514';
   END IF;
   IF NEW.decision IN ('verified','matched','recorded') AND EXISTS (
    SELECT 1 FROM clinic_app.billing_pspevidence e WHERE e.intent_id=intent.id
    AND NOT (e.source='statement' AND e.decision='quarantined')
    AND (e.version>NEW.version OR (e.version=NEW.version AND
     (e.status,e.amount_minor,e.currency,e.fee_minor) IS DISTINCT FROM
     (NEW.status,NEW.amount_minor,NEW.currency,NEW.fee_minor)))) THEN
    NEW.decision:='superseded';
   END IF;
  END IF;
 END IF;
 NEW.created_at:=clock_timestamp();
 RETURN NEW;
END $f$;
"""
