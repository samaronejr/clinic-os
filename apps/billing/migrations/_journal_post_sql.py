"""Guarded fact writers, separate from sealing and commit-time constraints."""

POST_SQL = """
CREATE FUNCTION clinic_app.billing_post_internal(
 requested_book uuid, fact_kind text, fact_id uuid, proposed_lines jsonb,
 reversed_entry uuid DEFAULT NULL)
RETURNS uuid LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE book clinic_app.billing_journalbook; existing clinic_app.billing_journalentry;
 normalized jsonb; fingerprint bytea; result uuid; item jsonb; account_id uuid;
BEGIN
 PERFORM clinic_app.billing_lock_book(requested_book);
 SELECT * INTO book FROM clinic_app.billing_journalbook
 WHERE id=requested_book FOR UPDATE;
 IF book.id IS NULL OR fact_id IS NULL OR fact_kind IS NULL
 OR fact_kind !~ '^[a-z_]{1,32}$' OR jsonb_typeof(proposed_lines)<>'array'
 OR jsonb_array_length(proposed_lines)<2 THEN
  RAISE EXCEPTION 'invalid journal input' USING ERRCODE='22023';
 END IF;
 SELECT jsonb_agg(value ORDER BY value::text COLLATE "C") INTO normalized
 FROM jsonb_array_elements(proposed_lines);
 fingerprint := sha256(convert_to(jsonb_build_object(
 'reversal',reversed_entry,'lines',normalized)::text,'UTF8'));
 SELECT * INTO existing FROM clinic_app.billing_journalentry
 WHERE book_id=requested_book AND source_kind=fact_kind AND source_id=fact_id;
 IF existing.id IS NOT NULL THEN
  IF existing.content_hash<>fingerprint OR existing.state<>'posted' THEN
   RAISE EXCEPTION 'journal fact conflict' USING ERRCODE='23514',
   CONSTRAINT='billing_fact_conflict';
  END IF;
  RETURN existing.id;
 END IF;
 IF reversed_entry IS NOT NULL AND NOT EXISTS (
 SELECT 1 FROM clinic_app.billing_journalentry e
 WHERE e.id=reversed_entry AND e.book_id=book.id AND e.state='posted') THEN
  RAISE EXCEPTION 'foreign reversal' USING ERRCODE='23514';
 END IF;
 result := gen_random_uuid();
 INSERT INTO clinic_app.billing_journalentry
 (id,organization_id,book_id,source_kind,source_id,content_hash,state,
 reversal_of_id,created_at) VALUES
 (result,book.organization_id,book.id,fact_kind,fact_id,fingerprint,'proposed',
 reversed_entry,statement_timestamp());
 FOR item IN SELECT value FROM jsonb_array_elements(normalized) LOOP
  IF jsonb_typeof(item)<>'object' OR
   item-'account'-'side'-'amount_minor'-'practitioner_id'-'owner_id'-'due_on'
   <> '{}'::jsonb OR item->>'amount_minor' IS NULL OR
   item->>'amount_minor' !~ '^[1-9][0-9]*$' THEN
   RAISE EXCEPTION 'invalid journal line' USING ERRCODE='22023';
  END IF;
  SELECT id INTO account_id FROM clinic_app.billing_account
  WHERE book_id=book.id AND code=item->>'account';
  INSERT INTO clinic_app.billing_journalline
  (id,organization_id,entry_id,account_id,side,amount_minor,
  practitioner_id,owner_id,due_on) VALUES
  (gen_random_uuid(),book.organization_id,result,account_id,item->>'side',
  (item->>'amount_minor')::bigint,(item->>'practitioner_id')::uuid,
  (item->>'owner_id')::uuid,(item->>'due_on')::date);
 END LOOP;
 UPDATE clinic_app.billing_journalentry SET state='posted' WHERE id=result;
 RETURN result;
END $f$;

CREATE FUNCTION clinic_app.billing_post(
 requested_book uuid, fact_kind text, fact_id uuid, proposed_lines jsonb,
 reversed_entry uuid)
RETURNS TABLE(entry_id uuid, changed boolean)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
BEGIN
 IF NOT clinic_app.billing_book_access(requested_book,'settlement.post')
 OR fact_kind<>'manual' THEN
  RAISE EXCEPTION 'billing access denied' USING ERRCODE='42501';
 END IF;
 PERFORM clinic_app.billing_lock_book(requested_book);
 changed := NOT EXISTS (SELECT 1 FROM clinic_app.billing_journalentry
 WHERE book_id=requested_book AND source_kind=fact_kind AND source_id=fact_id);
 entry_id := clinic_app.billing_post_internal(
 requested_book,fact_kind,fact_id,proposed_lines,reversed_entry);
 RETURN NEXT;
END $f$;

CREATE FUNCTION clinic_app.billing_opening_balance(
 requested_book uuid, requested_account text, amount bigint, evidence uuid)
RETURNS TABLE(entry_id uuid, changed boolean)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
BEGIN
 IF NOT clinic_app.billing_book_access(requested_book,'finance.policy') THEN
  RAISE EXCEPTION 'billing access denied' USING ERRCODE='42501';
 END IF;
 PERFORM clinic_app.billing_lock_book(requested_book);
 changed:=NOT EXISTS (SELECT 1 FROM clinic_app.billing_journalentry e
 WHERE e.book_id=requested_book AND e.source_kind='opening_balance'
 AND e.source_id=evidence);
 IF requested_account NOT IN ('bank','petty_cash','psp_clearing') OR amount<=0
 OR (changed AND EXISTS (SELECT 1 FROM clinic_app.billing_journalline l
 JOIN clinic_app.billing_account a ON a.id=l.account_id
 WHERE a.book_id=requested_book AND a.code=requested_account)) THEN
  RAISE EXCEPTION 'opening balance requires unused account' USING ERRCODE='23514';
 END IF;
 entry_id:=clinic_app.billing_post_internal(requested_book,'opening_balance',evidence,
  clinic_app.billing_line(requested_account,'debit',amount) ||
  clinic_app.billing_line('opening_balances','credit',amount));
 RETURN NEXT;
END $f$;

"""
