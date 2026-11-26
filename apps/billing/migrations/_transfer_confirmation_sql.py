"""The terminal transition and proof consumption succeed or roll back together."""

SQL = """
-- Serialize the historical scan with new confirmations. Inline FKs on the new
-- empty table avoid a validation pass around existing FORCE RLS policies.
LOCK TABLE clinic_app.billing_refund, clinic_app.billing_payout
 IN SHARE ROW EXCLUSIVE MODE;
CREATE TABLE clinic_app.billing_transferconfirmation (
 entry_id uuid PRIMARY KEY REFERENCES clinic_app.billing_journalentry(id),
 organization_id uuid NOT NULL REFERENCES clinic_app.identity_organization(id),
 book_id uuid NOT NULL REFERENCES clinic_app.billing_journalbook(id),
 evidence_reference uuid NOT NULL,
 CONSTRAINT billing_transfer_line_uniq UNIQUE(book_id,evidence_reference));
CREATE INDEX billing_transfer_org_idx
 ON clinic_app.billing_transferconfirmation(organization_id);
REVOKE ALL ON clinic_app.billing_transferconfirmation
 FROM PUBLIC,clinic_app,clinic_agent;
GRANT SELECT, INSERT ON clinic_app.billing_transferconfirmation TO clinic_resolver;
ALTER TABLE clinic_app.billing_transferconfirmation ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.billing_transferconfirmation FORCE ROW LEVEL SECURITY;
-- No runtime policy or grant. Finance reads the confirmed instruction and
-- posting, not the resolver's private cross-kind uniqueness index.

SET LOCAL ROLE clinic_resolver;
-- The OUTER join and non-null PK also refuse a confirmed historical
-- instruction missing its exact posting. Never silently resolve a duplicate.
INSERT INTO clinic_app.billing_transferconfirmation
 (entry_id,organization_id,book_id,evidence_reference)
 SELECT e.id,t.organization_id,t.book_id,t.evidence_reference FROM (
  SELECT id,organization_id,book_id,evidence_reference,'refund_confirmed' AS kind_key
   FROM clinic_app.billing_refund WHERE state='confirmed'
  UNION ALL SELECT id,organization_id,book_id,evidence_reference,'payout_confirmed'
   FROM clinic_app.billing_payout WHERE state='confirmed') t
 LEFT JOIN clinic_app.billing_journalentry e ON e.book_id=t.book_id
 AND e.organization_id=t.organization_id AND e.source_kind=t.kind_key
 AND e.source_id=t.id AND e.state='posted';

CREATE FUNCTION clinic_app.billing_bind_transfer()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE posting uuid;
BEGIN
 SELECT id INTO STRICT posting FROM clinic_app.billing_journalentry
 WHERE book_id=NEW.book_id AND organization_id=NEW.organization_id AND source_id=NEW.id
 AND state='posted' AND source_kind=CASE TG_TABLE_NAME
 WHEN 'billing_payout' THEN 'payout_confirmed'
 WHEN 'billing_refund' THEN 'refund_confirmed' END;
 INSERT INTO clinic_app.billing_transferconfirmation
 (entry_id,organization_id,book_id,evidence_reference)
 VALUES(posting,NEW.organization_id,NEW.book_id,NEW.evidence_reference);
 RETURN NULL;
END $f$;
REVOKE ALL ON FUNCTION clinic_app.billing_bind_transfer() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.billing_bind_transfer(),
 clinic_app.billing_immutable() TO clinic_owner;
RESET ROLE;
CREATE TRIGGER billing_bind_transfer AFTER UPDATE OF state ON clinic_app.billing_refund
 FOR EACH ROW WHEN (NEW.state='confirmed')
 EXECUTE FUNCTION clinic_app.billing_bind_transfer();
CREATE TRIGGER billing_bind_transfer AFTER UPDATE OF state ON clinic_app.billing_payout
 FOR EACH ROW WHEN (NEW.state='confirmed')
 EXECUTE FUNCTION clinic_app.billing_bind_transfer();
CREATE TRIGGER billing_history_immutable BEFORE UPDATE OR DELETE
 ON clinic_app.billing_transferconfirmation
 FOR EACH ROW EXECUTE FUNCTION clinic_app.billing_immutable();
CREATE TRIGGER billing_no_truncate BEFORE TRUNCATE
 ON clinic_app.billing_transferconfirmation
 FOR EACH STATEMENT EXECUTE FUNCTION clinic_app.billing_immutable();
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.billing_bind_transfer(),
 clinic_app.billing_immutable() FROM clinic_owner;
RESET ROLE;
"""
