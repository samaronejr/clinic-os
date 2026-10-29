"""New writes require the survivor without expanding historical UPDATE grants."""

SQL = """
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.intake_survivor_write_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'intake-alias-write-v1:'||NEW.organization_id::text||':'||NEW.patient_id::text,0
    ));
    IF EXISTS (
        SELECT 1 FROM clinic_app.intake_patientalias a
        WHERE a.organization_id=NEW.organization_id AND a.active
          AND a.merged_patient_id=NEW.patient_id
    ) THEN
        RAISE EXCEPTION USING ERRCODE='23514',
            CONSTRAINT='intake_patient_aliased_check',MESSAGE='patient_aliased';
    END IF;
    RETURN NEW;
END
$function$;
ALTER FUNCTION clinic_app.intake_survivor_write_guard() OWNER TO clinic_resolver;
REVOKE ALL ON FUNCTION clinic_app.intake_survivor_write_guard()
    FROM PUBLIC,clinic_app;
GRANT EXECUTE ON FUNCTION clinic_app.intake_survivor_write_guard() TO clinic_owner;
RESET ROLE;

-- Run after existing scope/capacity guards; keep their lock order and clauses.
CREATE TRIGGER zz_intake_survivor_binding
BEFORE INSERT ON clinic_app.scheduling_appointment
FOR EACH ROW EXECUTE FUNCTION clinic_app.intake_survivor_write_guard();
CREATE TRIGGER zz_intake_survivor_binding
BEFORE INSERT ON clinic_app.ehr_encounter
FOR EACH ROW EXECUTE FUNCTION clinic_app.intake_survivor_write_guard();
CREATE TRIGGER zz_intake_survivor_binding
BEFORE INSERT ON clinic_app.billing_invoice
FOR EACH ROW EXECUTE FUNCTION clinic_app.intake_survivor_write_guard();
SET LOCAL ROLE clinic_resolver;
REVOKE ALL ON FUNCTION clinic_app.intake_survivor_write_guard() FROM clinic_owner;
RESET ROLE;
"""
