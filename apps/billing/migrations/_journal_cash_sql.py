"""Cash counts are assertions against the sealed journal, never balancing plugs."""

CASH_SQL = """
CREATE FUNCTION clinic_app.billing_account_balance(
 requested_book uuid, requested_account text)
RETURNS numeric LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT coalesce(sum(CASE l.side WHEN 'debit' THEN l.amount_minor::numeric
 ELSE -l.amount_minor::numeric END),0)
 FROM clinic_app.billing_journalline l
 JOIN clinic_app.billing_account a ON a.id=l.account_id
 JOIN clinic_app.billing_journalentry e ON e.id=l.entry_id
 WHERE a.book_id=requested_book AND a.code=requested_account AND e.state='posted'
$f$;

CREATE FUNCTION clinic_app.billing_cash_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE book clinic_app.billing_journalbook; expected numeric;
BEGIN
 IF TG_OP='DELETE' THEN
  RAISE EXCEPTION 'cash history is immutable' USING ERRCODE='23514';
 END IF;
 PERFORM clinic_app.billing_lock_book(NEW.book_id);
 SELECT * INTO book FROM clinic_app.billing_journalbook WHERE id=NEW.book_id;
 IF book.organization_id IS DISTINCT FROM NEW.organization_id
 OR NOT clinic_app.billing_book_access(book.id,'charge.collect') THEN
  RAISE EXCEPTION 'billing access denied' USING ERRCODE='42501';
 END IF;
 expected:=clinic_app.billing_account_balance(book.id,'petty_cash');
 IF NEW.counted_minor IS NULL OR NEW.counted_minor<0
 OR NEW.counted_minor::numeric<>expected THEN
  RAISE EXCEPTION 'cash count has unexplained difference' USING ERRCODE='23514',
  CONSTRAINT='billing_cash_difference';
 END IF;
 IF TG_OP='INSERT' THEN
  IF NEW.state<>'open' OR NEW.revision<>1 OR NEW.reason<>''
  OR NEW.last_command_id IS DISTINCT FROM NEW.command_id THEN
   RAISE EXCEPTION 'invalid cash opening' USING ERRCODE='23514';
  END IF;
  NEW.created_at:=statement_timestamp();
 ELSE
  IF to_jsonb(NEW)-'state'-'counted_minor'-'reason'-'last_command_id'
   <>to_jsonb(OLD)-'state'-'counted_minor'-'reason'-'last_command_id' THEN
   RAISE EXCEPTION 'cash scope is immutable' USING ERRCODE='23514';
  END IF;
  IF OLD.state='open' AND NEW.state='closed' THEN
   IF EXISTS (SELECT 1 FROM clinic_app.billing_journalentry e
    WHERE e.book_id=book.id AND e.state<>'posted')
   OR EXISTS (SELECT 1 FROM clinic_app.billing_journalsettlement s
    WHERE s.book_id=book.id AND s.state<>'posted')
   OR EXISTS (SELECT 1 FROM clinic_app.billing_refund r
    WHERE r.book_id=book.id AND r.state='unknown')
   OR EXISTS (SELECT 1 FROM clinic_app.billing_payout p
    WHERE p.book_id=book.id AND p.state='unknown')
   OR EXISTS (SELECT 1 FROM clinic_app.billing_journalline l
    JOIN clinic_app.billing_account a ON a.id=l.account_id
    JOIN clinic_app.billing_journalentry e ON e.id=l.entry_id
    WHERE a.book_id=book.id AND a.code='suspense' AND e.state='posted'
    GROUP BY l.owner_id,l.due_on
    HAVING sum(CASE l.side WHEN 'debit' THEN l.amount_minor::numeric
     ELSE -l.amount_minor::numeric END)<>0)
   OR clinic_app.billing_account_balance(book.id,'unidentified_receipts')<>0 THEN
    RAISE EXCEPTION 'cash close has unresolved exceptions' USING ERRCODE='23514',
    CONSTRAINT='billing_cash_exceptions';
   END IF;
   IF NEW.reason<>'' THEN
    RAISE EXCEPTION 'invalid cash close reason' USING ERRCODE='23514';
   END IF;
  ELSIF OLD.state='closed' AND NEW.state='open' THEN
   IF NEW.reason NOT IN ('correction','reconciliation')
   OR NOT clinic_app.billing_book_access(book.id,'settlement.post') THEN
    RAISE EXCEPTION 'billing access denied' USING ERRCODE='42501';
   END IF;
   PERFORM clinic_app.billing_finance_session(book.clinic_id);
  ELSE
   RAISE EXCEPTION 'invalid cash transition' USING ERRCODE='23514';
  END IF;
  NEW.revision:=OLD.revision+1;
 END IF;
 NEW.expected_minor:=expected::bigint;
 RETURN NEW;
END $f$;

CREATE FUNCTION clinic_app.billing_cash_receipt()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE next_state text; proof uuid;
BEGIN
 IF TG_OP='INSERT' THEN next_state:='opened';
 ELSIF NEW.state='closed' THEN next_state:='closed';
 ELSE
  next_state:='reopened';
  proof:=clinic_app.billing_finance_session(
   (SELECT clinic_id FROM clinic_app.billing_journalbook WHERE id=NEW.book_id));
 END IF;
 INSERT INTO clinic_app.billing_cashsessionevent
 (id,organization_id,book_id,session_id,revision,command_id,state,counted_minor,
 expected_minor,reason,actor_id,verification_session_id,created_at) VALUES
 (gen_random_uuid(),NEW.organization_id,NEW.book_id,NEW.id,
 NEW.revision,NEW.last_command_id,
 next_state,NEW.counted_minor,NEW.expected_minor,NEW.reason,
 NULLIF(current_setting('app.current_user_id',true),'')::uuid,proof,
 statement_timestamp());
 RETURN NULL;
END $f$;
"""
