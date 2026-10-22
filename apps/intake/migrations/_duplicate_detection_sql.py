"""Exact retained-history and same-clinic duplicate-index binding guards."""

SQL = """
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.intake_duplicate_detection_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    patient_a uuid;
    patient_b uuid;
    latest_version integer;
BEGIN
    IF NEW.organization_id IS DISTINCT FROM NULLIF(
        current_setting('app.current_tenant', true), ''
    )::uuid OR NOT (
        clinic_app.has_permission('demographics.read',NEW.clinic_id,NULL)
        OR clinic_app.questionnaire_staff(
            NEW.clinic_id,ARRAY['owner','clinic_admin','receptionist','physician']
        )
    ) OR NULLIF(current_setting('app.current_principal', true), '') IS NOT NULL
    THEN
        RAISE EXCEPTION USING ERRCODE='42501',
            MESSAGE='duplicate review authority required';
    END IF;
    IF TG_TABLE_NAME='intake_patientduplicateindex' THEN
        IF NOT EXISTS (
            SELECT 1 FROM clinic_app.intake_patientclinicenrollment e
            WHERE e.organization_id=NEW.organization_id
              AND e.clinic_id=NEW.clinic_id AND e.patient_id=NEW.patient_id
        ) THEN
            RAISE EXCEPTION USING ERRCODE='23514',
                MESSAGE='duplicate index must bind an enrolled patient';
        END IF;
        SELECT COALESCE(max(i.version),0) INTO latest_version
        FROM clinic_app.intake_patientduplicateindex i
        WHERE i.organization_id=NEW.organization_id
          AND i.patient_id=NEW.patient_id;
        IF NEW.version IS DISTINCT FROM latest_version+1
           OR (NEW.name_birth_index IS NOT NULL
               AND octet_length(NEW.name_birth_index)<>32)
           OR EXISTS (SELECT 1 FROM unnest(NEW.contact_indexes) i
                      WHERE i IS NULL OR octet_length(i)<>32)
        THEN
            RAISE EXCEPTION USING ERRCODE='23514',
                MESSAGE='duplicate index version is invalid';
        END IF;
    ELSE
        SELECT e.patient_id INTO patient_a
        FROM clinic_app.intake_patientclinicenrollment e
        WHERE e.organization_id=NEW.organization_id
          AND e.clinic_id=NEW.clinic_id AND e.id=NEW.enrollment_a_id;
        SELECT e.patient_id INTO patient_b
        FROM clinic_app.intake_patientclinicenrollment e
        WHERE e.organization_id=NEW.organization_id
          AND e.clinic_id=NEW.clinic_id AND e.id=NEW.enrollment_b_id;
        IF patient_a IS NULL OR patient_b IS NULL OR patient_a=patient_b
           OR NEW.state<>'open'
           OR cardinality(NEW.signals)=0
           OR NOT NEW.signals <@ ARRAY[
               'exact_identifier','dob+name_prefix','contact'
           ]::varchar[]
        THEN
            RAISE EXCEPTION USING ERRCODE='23514',
                MESSAGE='duplicate candidate binding is invalid';
        END IF;
    END IF;
    NEW.created_at := transaction_timestamp();
    RETURN NEW;
END
$function$;
ALTER FUNCTION clinic_app.intake_duplicate_detection_guard()
    OWNER TO clinic_resolver;
REVOKE ALL ON FUNCTION clinic_app.intake_duplicate_detection_guard()
    FROM PUBLIC, clinic_app;
GRANT EXECUTE ON FUNCTION clinic_app.intake_duplicate_detection_guard()
    TO clinic_owner;
RESET ROLE;

CREATE FUNCTION clinic_app.intake_duplicate_history_guard()
RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    RAISE EXCEPTION USING ERRCODE='23514',
        MESSAGE='duplicate review history is retained';
END
$function$;
REVOKE ALL ON FUNCTION clinic_app.intake_duplicate_history_guard()
    FROM PUBLIC, clinic_app, clinic_resolver;
"""

FINALIZE_SQL = """
SET LOCAL ROLE clinic_resolver;
REVOKE ALL ON FUNCTION clinic_app.intake_duplicate_detection_guard()
    FROM clinic_owner;
RESET ROLE;
"""


def table_sql(table: str) -> str:
    """Install literal relation guards and tenant-composite bindings."""
    if table not in {"intake_duplicatecandidate", "intake_patientduplicateindex"}:
        raise KeyError(table)
    patient_fk = (
        f"""
ALTER TABLE clinic_app.{table}
ADD CONSTRAINT {table}_org_patient_fk
FOREIGN KEY (organization_id,patient_id)
REFERENCES clinic_app.intake_patient(organization_id,id) NOT DEFERRABLE;
"""
        if table == "intake_patientduplicateindex"
        else ""
    )
    return f"""
GRANT SELECT ON TABLE clinic_app.{table} TO clinic_resolver;
ALTER TABLE clinic_app.{table}
ADD CONSTRAINT {table}_org_clinic_fk
FOREIGN KEY (organization_id,clinic_id)
REFERENCES clinic_app.identity_clinic(organization_id,id) NOT DEFERRABLE;
{patient_fk}
CREATE TRIGGER {table}_binding
BEFORE INSERT ON clinic_app.{table}
FOR EACH ROW EXECUTE FUNCTION clinic_app.intake_duplicate_detection_guard();
CREATE TRIGGER {table}_retained
BEFORE UPDATE OR DELETE ON clinic_app.{table}
FOR EACH ROW EXECUTE FUNCTION clinic_app.intake_duplicate_history_guard();
"""
