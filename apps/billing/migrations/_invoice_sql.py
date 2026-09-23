"""Database-owned invoice history, exact-scope RLS and settlement receipts."""

TABLES = ("invoice", "invoicerevision", "settlement", "receipt")

_sql = """
GRANT SELECT ON clinic_app.billing_invoice, clinic_app.billing_invoicerevision,
 clinic_app.billing_settlement, clinic_app.billing_receipt TO clinic_resolver;
GRANT INSERT ON clinic_app.billing_invoicerevision, clinic_app.billing_receipt
 TO clinic_resolver;
GRANT UPDATE (state) ON clinic_app.billing_invoice TO clinic_resolver;
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.billing_staff_invoice(requested_invoice uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
 SELECT EXISTS (SELECT 1 FROM clinic_app.billing_invoice i
   WHERE i.id = requested_invoice AND i.organization_id =
     NULLIF(current_setting('app.current_tenant', true), '')::uuid
   AND clinic_app.questionnaire_staff(i.clinic_id,
       ARRAY['owner','clinic_admin','receptionist']))
$f$;
CREATE FUNCTION clinic_app.billing_patient_charges()
RETURNS TABLE(invoice_id uuid, reference uuid, amount_minor bigint,
 currency varchar, state varchar, issued_at timestamptz,
 receipt_reference uuid, receipt_issued_at timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
 SELECT i.id, i.reference, i.amount_minor, i.currency, i.state, i.issued_at,
   r.reference, r.issued_at
 FROM clinic_app.billing_invoice i
 LEFT JOIN clinic_app.billing_receipt r ON r.invoice_id = i.id
 WHERE i.released_at IS NOT NULL AND i.issued_at IS NOT NULL
   AND EXISTS (SELECT 1 FROM clinic_app.intake_patientsession s
     JOIN clinic_app.intake_patientaccessgrant g ON g.id = s.grant_id
     WHERE s.id =
       NULLIF(current_setting('app.current_patient_session', true), '')::uuid
     AND s.organization_id = i.organization_id AND s.clinic_id = i.clinic_id
     AND s.patient_id = i.patient_id
     AND s.revoked_at IS NULL AND g.revoked_at IS NULL
     AND s.expires_at > statement_timestamp()
     AND s.idle_expires_at > statement_timestamp()
     AND 'billing' = ANY(s.operations))
 ORDER BY i.created_at, i.id
$f$;
CREATE FUNCTION clinic_app.billing_immutable()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
BEGIN
 RAISE EXCEPTION 'billing history is immutable' USING ERRCODE = '23514';
END
$f$;
CREATE FUNCTION clinic_app.billing_invoice_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
BEGIN
 IF TG_OP = 'INSERT' THEN
   IF NEW.state <> 'draft' OR NEW.revision <> 1
      OR NEW.issued_at IS NOT NULL OR NEW.released_at IS NOT NULL
      OR NOT EXISTS (SELECT 1 FROM clinic_app.intake_patientclinicenrollment n
        WHERE n.organization_id = NEW.organization_id
        AND n.clinic_id = NEW.clinic_id AND n.patient_id = NEW.patient_id)
      OR (NEW.appointment_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM clinic_app.scheduling_appointment a
        WHERE a.id = NEW.appointment_id AND a.organization_id = NEW.organization_id
        AND a.clinic_id = NEW.clinic_id AND a.patient_id = NEW.patient_id))
      OR (NEW.encounter_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM clinic_app.ehr_encounter e
        WHERE e.id = NEW.encounter_id AND e.organization_id = NEW.organization_id
        AND e.clinic_id = NEW.clinic_id AND e.patient_id = NEW.patient_id
        AND (NEW.appointment_id IS NULL OR e.appointment_id = NEW.appointment_id)))
   THEN
     RAISE EXCEPTION 'invalid invoice binding' USING ERRCODE = '23514';
   END IF;
 ELSE
   IF ROW(NEW.id, NEW.organization_id, NEW.clinic_id, NEW.patient_id,
          NEW.appointment_id, NEW.encounter_id, NEW.reference, NEW.created_at)
      IS DISTINCT FROM ROW(OLD.id, OLD.organization_id, OLD.clinic_id,
          OLD.patient_id, OLD.appointment_id, OLD.encounter_id,
          OLD.reference, OLD.created_at)
      OR (OLD.issued_at IS NOT NULL AND
          NEW.issued_at IS DISTINCT FROM OLD.issued_at)
      OR (OLD.released_at IS NOT NULL AND
          NEW.released_at IS DISTINCT FROM OLD.released_at)
   THEN
     RAISE EXCEPTION 'immutable invoice binding' USING ERRCODE = '23514';
   END IF;
   IF OLD.state = 'draft' AND NEW.state = 'draft' THEN
     IF NEW.revision <> OLD.revision + 1 THEN
       RAISE EXCEPTION 'invalid draft revision' USING ERRCODE = '23514';
     END IF;
   ELSIF ROW(NEW.amount_minor, NEW.currency, NEW.revision)
       IS DISTINCT FROM ROW(OLD.amount_minor, OLD.currency, OLD.revision) THEN
     RAISE EXCEPTION 'immutable issued terms' USING ERRCODE = '23514';
   END IF;
   IF NEW.state IS DISTINCT FROM OLD.state AND NOT (
       (OLD.state = 'draft' AND NEW.state IN ('open','cancelled'))
       OR (OLD.state = 'open' AND NEW.state = 'cancelled')
       OR (OLD.state = 'open' AND NEW.state = 'paid' AND EXISTS (
           SELECT 1 FROM clinic_app.billing_settlement s
           WHERE s.invoice_id = OLD.id AND s.organization_id = OLD.organization_id
             AND s.amount_minor = OLD.amount_minor AND s.currency = OLD.currency)))
   THEN
     RAISE EXCEPTION 'invalid invoice transition' USING ERRCODE = '23514';
   END IF;
   IF OLD.issued_at IS NULL AND NEW.issued_at IS NOT NULL
      AND NOT (OLD.state = 'draft' AND NEW.state = 'open') THEN
     RAISE EXCEPTION 'invalid invoice issue' USING ERRCODE = '23514';
   END IF;
 END IF;
 RETURN NEW;
END
$f$;
CREATE FUNCTION clinic_app.billing_revision_snapshot()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
BEGIN
 IF NEW.state = 'draft' THEN
   INSERT INTO clinic_app.billing_invoicerevision
     (id, organization_id, invoice_id, revision, amount_minor, currency,
      reference, actor_id, created_at)
   VALUES (gen_random_uuid(), NEW.organization_id, NEW.id, NEW.revision,
      NEW.amount_minor, NEW.currency, NEW.reference,
      NULLIF(current_setting('app.current_user_id', true), '')::uuid,
      statement_timestamp());
 END IF;
 RETURN NEW;
END
$f$;
CREATE FUNCTION clinic_app.billing_settlement_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
DECLARE
 charge clinic_app.billing_invoice;
BEGIN
 SELECT * INTO charge FROM clinic_app.billing_invoice
   WHERE id = NEW.invoice_id FOR UPDATE;
 IF charge.id IS NULL OR charge.organization_id <> NEW.organization_id
    OR charge.state <> 'open' OR charge.amount_minor <> NEW.amount_minor
    OR charge.currency <> NEW.currency
    OR NEW.confirmed_by_id IS DISTINCT FROM
       NULLIF(current_setting('app.current_user_id', true), '')::uuid
    OR NOT clinic_app.questionnaire_staff(charge.clinic_id,
      ARRAY['owner','clinic_admin','receptionist']) THEN
   RAISE EXCEPTION 'invalid confirmed settlement' USING ERRCODE = '23514';
 END IF;
 NEW.confirmed_at := statement_timestamp();
 RETURN NEW;
END
$f$;
CREATE FUNCTION clinic_app.billing_settlement_receipt()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
BEGIN
 INSERT INTO clinic_app.billing_receipt
   (id, organization_id, settlement_id, invoice_id, reference,
    amount_minor, currency, issued_at)
 VALUES (gen_random_uuid(), NEW.organization_id, NEW.id, NEW.invoice_id,
   gen_random_uuid(), NEW.amount_minor, NEW.currency, NEW.confirmed_at);
 UPDATE clinic_app.billing_invoice SET state = 'paid' WHERE id = NEW.invoice_id;
 RETURN NEW;
END
$f$;
REVOKE ALL ON FUNCTION clinic_app.billing_staff_invoice(uuid),
 clinic_app.billing_patient_charges(), clinic_app.billing_immutable(),
 clinic_app.billing_invoice_guard(), clinic_app.billing_revision_snapshot(),
 clinic_app.billing_settlement_guard(), clinic_app.billing_settlement_receipt()
 FROM PUBLIC, clinic_app;
GRANT EXECUTE ON FUNCTION clinic_app.billing_staff_invoice(uuid),
 clinic_app.billing_patient_charges() TO clinic_app;
GRANT EXECUTE ON FUNCTION clinic_app.billing_immutable(),
 clinic_app.billing_invoice_guard(), clinic_app.billing_revision_snapshot(),
 clinic_app.billing_settlement_guard(), clinic_app.billing_settlement_receipt()
 TO clinic_owner;
RESET ROLE;
CREATE TRIGGER billing_invoice_guard BEFORE INSERT OR UPDATE
 ON clinic_app.billing_invoice FOR EACH ROW
 EXECUTE FUNCTION clinic_app.billing_invoice_guard();
CREATE TRIGGER billing_revision_snapshot AFTER INSERT OR UPDATE
 ON clinic_app.billing_invoice FOR EACH ROW
 EXECUTE FUNCTION clinic_app.billing_revision_snapshot();
CREATE TRIGGER billing_settlement_guard BEFORE INSERT
 ON clinic_app.billing_settlement FOR EACH ROW
 EXECUTE FUNCTION clinic_app.billing_settlement_guard();
CREATE TRIGGER billing_settlement_receipt AFTER INSERT
 ON clinic_app.billing_settlement FOR EACH ROW
 EXECUTE FUNCTION clinic_app.billing_settlement_receipt();
REVOKE ALL ON clinic_app.billing_invoice, clinic_app.billing_invoicerevision,
 clinic_app.billing_settlement, clinic_app.billing_receipt FROM PUBLIC, clinic_app;
GRANT SELECT, INSERT ON clinic_app.billing_invoice,
 clinic_app.billing_settlement TO clinic_app;
GRANT SELECT ON clinic_app.billing_invoicerevision,
 clinic_app.billing_receipt TO clinic_app;
GRANT UPDATE (amount_minor, currency, revision, state, issued_at, released_at)
 ON clinic_app.billing_invoice TO clinic_app;
"""

for table in TABLES:
    predicate = (
        "clinic_app.questionnaire_staff(clinic_id, "
        "ARRAY['owner','clinic_admin','receptionist'])"
        if table == "invoice"
        else "clinic_app.billing_staff_invoice(invoice_id)"
    )
    scope = (
        "organization_id = "
        "NULLIF(current_setting('app.current_tenant', true), '')::uuid "
        f"AND {predicate}"
    )
    _sql += f"""
ALTER TABLE clinic_app.billing_{table} ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.billing_{table} FORCE ROW LEVEL SECURITY;
CREATE POLICY billing_staff ON clinic_app.billing_{table}
 USING ({scope}) WITH CHECK ({scope});
CREATE TRIGGER billing_no_delete BEFORE DELETE ON clinic_app.billing_{table}
 FOR EACH ROW EXECUTE FUNCTION clinic_app.billing_immutable();
"""
    if table != "invoice":
        _sql += f"""
CREATE TRIGGER billing_no_update BEFORE UPDATE ON clinic_app.billing_{table}
 FOR EACH ROW EXECUTE FUNCTION clinic_app.billing_immutable();
"""

_sql += """
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.billing_immutable(),
 clinic_app.billing_invoice_guard(), clinic_app.billing_revision_snapshot(),
 clinic_app.billing_settlement_guard(), clinic_app.billing_settlement_receipt()
 FROM clinic_owner;
RESET ROLE;
"""
SQL = _sql

_reverse_sql = ""
for table in TABLES:
    if table != "invoice":
        _reverse_sql += (
            f"DROP TRIGGER billing_no_update ON clinic_app.billing_{table};\n"
        )
    _reverse_sql += f"""
DROP TRIGGER billing_no_delete ON clinic_app.billing_{table};
DROP POLICY billing_staff ON clinic_app.billing_{table};
ALTER TABLE clinic_app.billing_{table} NO FORCE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.billing_{table} DISABLE ROW LEVEL SECURITY;
REVOKE ALL ON clinic_app.billing_{table} FROM clinic_app, clinic_resolver;
"""
_reverse_sql += """
REVOKE UPDATE (amount_minor, currency, revision, state, issued_at, released_at)
 ON clinic_app.billing_invoice FROM clinic_app;
REVOKE UPDATE (state) ON clinic_app.billing_invoice FROM clinic_resolver;
DROP TRIGGER billing_settlement_receipt ON clinic_app.billing_settlement;
DROP TRIGGER billing_settlement_guard ON clinic_app.billing_settlement;
DROP TRIGGER billing_revision_snapshot ON clinic_app.billing_invoice;
DROP TRIGGER billing_invoice_guard ON clinic_app.billing_invoice;
SET LOCAL ROLE clinic_resolver;
DROP FUNCTION clinic_app.billing_settlement_receipt();
DROP FUNCTION clinic_app.billing_settlement_guard();
DROP FUNCTION clinic_app.billing_revision_snapshot();
DROP FUNCTION clinic_app.billing_invoice_guard();
DROP FUNCTION clinic_app.billing_immutable();
DROP FUNCTION clinic_app.billing_patient_charges();
DROP FUNCTION clinic_app.billing_staff_invoice(uuid);
RESET ROLE;
"""
REVERSE_SQL = _reverse_sql
