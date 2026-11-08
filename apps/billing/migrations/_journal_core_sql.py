"""Sealing, commit-time balance and one posting per economic fact."""

CORE_SQL = """
CREATE FUNCTION clinic_app.billing_finance_read(requested_clinic uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT NULLIF(current_setting('app.current_patient_session',true),'') IS NULL
 AND NULLIF(current_setting('app.current_principal',true),'') IS NULL
 AND EXISTS (SELECT 1 FROM unnest(ARRAY['charge.collect','settlement.post',
 'refund.request','refund.approve','payout.request',
 'payout.approve','finance.policy']) p
 WHERE clinic_app.has_permission(p,requested_clinic,NULL))
$f$;

CREATE FUNCTION clinic_app.billing_lock_book(requested_book uuid)
RETURNS void LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE clinic uuid; tenant uuid;
BEGIN
 SELECT clinic_id,organization_id INTO clinic,tenant
 FROM clinic_app.billing_journalbook WHERE id=requested_book;
 IF clinic IS NULL OR tenant IS DISTINCT FROM
 NULLIF(current_setting('app.current_tenant',true),'')::uuid
 OR NOT (clinic_app.has_permission('charge.read',clinic,NULL)
 OR clinic_app.has_permission('appointment.move_own',clinic,NULL)) THEN
  RAISE EXCEPTION 'billing access denied' USING ERRCODE='42501';
 END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended(
 'clinic-lock-v1:clinic:'||clinic::text,0));
 PERFORM pg_advisory_xact_lock(hashtextextended('clinic-audit:'||tenant::text,0));
 PERFORM 1 FROM clinic_app.billing_journalbook WHERE id=requested_book FOR UPDATE;
END $f$;

CREATE FUNCTION clinic_app.billing_lock_clinic_book(requested_clinic uuid)
RETURNS void LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE tenant uuid;
BEGIN
 IF NULLIF(current_setting('app.current_patient_session',true),'') IS NOT NULL
 OR NULLIF(current_setting('app.current_principal',true),'') IS NOT NULL THEN
  RAISE EXCEPTION 'billing access denied' USING ERRCODE='42501';
 END IF;
 SELECT organization_id INTO tenant FROM clinic_app.identity_clinic
 WHERE id=requested_clinic AND organization_id=
 NULLIF(current_setting('app.current_tenant',true),'')::uuid;
 IF tenant IS NULL THEN
  RAISE EXCEPTION 'billing access denied' USING ERRCODE='42501';
 END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended(
 'clinic-lock-v1:clinic:'||requested_clinic::text,0));
 PERFORM pg_advisory_xact_lock(hashtextextended('clinic-audit:'||tenant::text,0));
 PERFORM 1 FROM clinic_app.billing_journalbook
 WHERE clinic_id=requested_clinic FOR UPDATE;
END $f$;

CREATE FUNCTION clinic_app.billing_book_access(requested_book uuid, permission text)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT EXISTS (SELECT 1 FROM clinic_app.billing_journalbook b
 WHERE b.id=requested_book AND b.organization_id=
 NULLIF(current_setting('app.current_tenant',true),'')::uuid
 AND NULLIF(current_setting('app.current_patient_session',true),'') IS NULL
 AND NULLIF(current_setting('app.current_principal',true),'') IS NULL
 AND (CASE WHEN permission='journal.read'
 THEN clinic_app.billing_finance_read(b.clinic_id)
 AND clinic_app.has_permission('charge.read',b.clinic_id,NULL)
 ELSE clinic_app.has_permission(permission,b.clinic_id,NULL) END))
$f$;

CREATE FUNCTION clinic_app.billing_entry_readable(requested_entry uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT EXISTS (SELECT 1 FROM clinic_app.billing_journalentry e
 WHERE e.id=requested_entry
 AND clinic_app.billing_book_access(e.book_id,'journal.read'))
$f$;

CREATE FUNCTION clinic_app.billing_practitioners(requested_clinic uuid)
RETURNS TABLE(user_id uuid, display_label text)
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT DISTINCT u.id,u.username::text FROM clinic_app.identity_user u
 WHERE clinic_app.billing_finance_read(requested_clinic)
 AND clinic_app.has_permission('charge.read',requested_clinic,NULL)
 AND (EXISTS (SELECT 1 FROM clinic_app.identity_userclinicrole r
 WHERE r.clinic_id=requested_clinic AND r.user_id=u.id
 AND r.role IN ('physician','allied_professional'))
 OR EXISTS (SELECT 1 FROM clinic_app.billing_practitionerpayoutrule p
 JOIN clinic_app.billing_journalbook b ON b.id=p.book_id
 WHERE b.clinic_id=requested_clinic AND p.practitioner_id=u.id))
 ORDER BY u.username::text,u.id
$f$;

CREATE FUNCTION clinic_app.billing_finance_session(requested_clinic uuid)
RETURNS uuid LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE digest text; result uuid;
BEGIN
 digest := current_setting('app.current_finance_session',true);
 IF digest IS NULL OR digest !~ '^[0-9a-f]{64}$' THEN
  RAISE EXCEPTION 'financial step up required' USING ERRCODE='42501';
 END IF;
 IF NOT clinic_app.identity_session_verified(decode(digest,'hex')) THEN
  RAISE EXCEPTION 'financial step up required' USING ERRCODE='42501';
 END IF;
 SELECT s.id INTO result FROM clinic_app.identity_sessionrecord s
 WHERE s.session_key_hash=decode(digest,'hex');
 IF result IS NULL OR NOT EXISTS (SELECT 1 FROM clinic_app.identity_clinic c
 WHERE c.id=requested_clinic AND c.organization_id=
 NULLIF(current_setting('app.current_tenant',true),'')::uuid) THEN
  RAISE EXCEPTION 'financial step up required' USING ERRCODE='42501';
 END IF;
 RETURN result;
END $f$;

CREATE FUNCTION clinic_app.billing_round_ratio(numerator numeric, denominator numeric)
RETURNS bigint LANGUAGE plpgsql IMMUTABLE
SET search_path=pg_catalog,pg_temp AS $f$
DECLARE quotient numeric; remainder numeric;
BEGIN
 IF numerator IS NULL OR denominator IS NULL OR numerator<0 OR denominator<=0
 OR numerator<>trunc(numerator) OR denominator<>trunc(denominator) THEN
  RAISE EXCEPTION 'invalid exact ratio' USING ERRCODE='22023';
 END IF;
 quotient := div(numerator,denominator);
 remainder := mod(numerator,denominator);
 IF remainder*2>denominator OR
 (remainder*2=denominator AND mod(quotient,2)=1) THEN
  quotient := quotient+1;
 END IF;
 RETURN quotient::bigint;
END $f$;

CREATE FUNCTION clinic_app.billing_line(
 code text, side text, amount bigint, practitioner uuid DEFAULT NULL)
RETURNS jsonb LANGUAGE sql IMMUTABLE
SET search_path=pg_catalog,pg_temp AS $f$
 SELECT CASE WHEN amount=0 THEN '[]'::jsonb ELSE jsonb_build_array(
 jsonb_build_object('account',code,'side',side,'amount_minor',amount::text,
 'practitioner_id',practitioner,'owner_id',NULL,'due_on',NULL)) END
$f$;

CREATE FUNCTION clinic_app.billing_journal_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE entry clinic_app.billing_journalentry; account clinic_app.billing_account;
BEGIN
 IF TG_OP='DELETE' OR (TG_TABLE_NAME='billing_journalline' AND TG_OP='UPDATE')
 THEN RAISE EXCEPTION 'sealed journal is immutable' USING ERRCODE='23514'; END IF;
 IF TG_TABLE_NAME='billing_journalentry' THEN
  PERFORM clinic_app.billing_lock_book(NEW.book_id);
  IF NOT EXISTS (SELECT 1 FROM clinic_app.billing_journalbook b
  WHERE b.id=NEW.book_id AND b.organization_id=NEW.organization_id)
  OR (NEW.reversal_of_id IS NOT NULL AND NOT EXISTS (
  SELECT 1 FROM clinic_app.billing_journalentry e WHERE e.id=NEW.reversal_of_id
  AND e.book_id=NEW.book_id AND e.state='posted')) THEN
   RAISE EXCEPTION 'invalid journal scope' USING ERRCODE='23514';
  END IF;
  IF TG_OP='INSERT' THEN
   IF NEW.state<>'proposed' OR NEW.posted_at IS NOT NULL THEN
    RAISE EXCEPTION 'entry must be proposed' USING ERRCODE='23514';
   END IF;
  ELSE
   IF OLD.state<>'proposed' OR NEW.state<>'posted'
   OR to_jsonb(NEW)-'state'-'posted_at'<>to_jsonb(OLD)-'state'-'posted_at' THEN
    RAISE EXCEPTION 'sealed journal is immutable' USING ERRCODE='23514';
   END IF;
   NEW.posted_at := statement_timestamp();
  END IF;
 ELSE
  SELECT * INTO entry FROM clinic_app.billing_journalentry
  WHERE id=NEW.entry_id FOR UPDATE;
  SELECT * INTO account FROM clinic_app.billing_account WHERE id=NEW.account_id;
  IF entry.id IS NULL OR account.id IS NULL OR entry.state<>'proposed'
  OR entry.book_id<>account.book_id OR NEW.organization_id<>entry.organization_id
  OR account.organization_id<>entry.organization_id OR NEW.amount_minor<=0
  OR NEW.side NOT IN ('debit','credit') THEN
   RAISE EXCEPTION 'invalid journal line' USING ERRCODE='23514';
  END IF;
  IF account.code='practitioner_payables' AND NEW.practitioner_id IS NULL THEN
   RAISE EXCEPTION 'practitioner dimension required' USING ERRCODE='23514';
  END IF;
  IF account.code='suspense' AND (NEW.owner_id IS NULL OR NEW.due_on IS NULL) THEN
   RAISE EXCEPTION 'suspense owner and aging required' USING ERRCODE='23514';
  END IF;
  IF NEW.owner_id IS NOT NULL AND NOT (
   EXISTS (SELECT 1 FROM clinic_app.identity_userclinicrole r
    JOIN clinic_app.identity_user u ON u.id=r.user_id AND u.is_active
    JOIN clinic_app.billing_journalbook b ON b.clinic_id=r.clinic_id
    WHERE b.id=entry.book_id AND r.user_id=NEW.owner_id)
   OR EXISTS (SELECT 1 FROM clinic_app.billing_journalline l
    WHERE l.entry_id=entry.reversal_of_id AND l.account_id=NEW.account_id
    AND l.owner_id=NEW.owner_id AND l.due_on IS NOT DISTINCT FROM NEW.due_on
    AND l.side<>NEW.side AND l.amount_minor>=NEW.amount_minor)
  ) THEN
   RAISE EXCEPTION 'invalid suspense owner scope' USING ERRCODE='23514';
  END IF;
  IF NEW.practitioner_id IS NOT NULL AND NOT (
   EXISTS (SELECT 1 FROM clinic_app.identity_userclinicrole r
    JOIN clinic_app.billing_journalbook b ON b.clinic_id=r.clinic_id
    WHERE b.id=entry.book_id AND r.user_id=NEW.practitioner_id
    AND r.role IN ('physician','allied_professional'))
   OR EXISTS (SELECT 1 FROM clinic_app.billing_practitionerpayoutrule p
    WHERE p.book_id=entry.book_id AND p.practitioner_id=NEW.practitioner_id)
   OR EXISTS (SELECT 1 FROM clinic_app.billing_journalline l
    WHERE l.entry_id=entry.reversal_of_id AND l.practitioner_id=NEW.practitioner_id)
  ) THEN
   RAISE EXCEPTION 'invalid practitioner dimension scope' USING ERRCODE='23514';
  END IF;
  IF account.code='petty_cash' AND entry.source_kind<>'opening_balance' AND NOT EXISTS (
  SELECT 1 FROM clinic_app.billing_cashsession c
  WHERE c.book_id=entry.book_id AND c.state='open') THEN
   RAISE EXCEPTION 'cash drawer is closed' USING ERRCODE='23514';
  END IF;
 END IF;
 RETURN NEW;
END $f$;

CREATE FUNCTION clinic_app.billing_entry_balance()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE requested_entry uuid; entry_state text; delta numeric; rows bigint;
BEGIN
 IF TG_TABLE_NAME='billing_journalline' THEN requested_entry:=NEW.entry_id;
 ELSE requested_entry:=NEW.id; END IF;
 SELECT state INTO entry_state FROM clinic_app.billing_journalentry
 WHERE id=requested_entry;
 IF entry_state='posted' THEN
  SELECT count(*),sum(CASE WHEN side='debit' THEN amount_minor::numeric
  ELSE -amount_minor::numeric END) INTO rows,delta
  FROM clinic_app.billing_journalline WHERE entry_id=requested_entry;
  IF rows<2 OR delta IS DISTINCT FROM 0::numeric THEN
   RAISE EXCEPTION 'journal entry is unbalanced' USING ERRCODE='23514',
   CONSTRAINT='billing_entry_balanced';
  END IF;
 END IF;
 RETURN NULL;
END $f$;
"""
