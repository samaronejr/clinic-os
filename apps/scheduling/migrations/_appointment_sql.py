"""Frozen SQL boundaries for the appointment schema migration."""

INTEGRITY_SQL = """
ALTER TABLE clinic_app.scheduling_appointment
    ADD CONSTRAINT scheduling_appointment_org_clinic_fk
    FOREIGN KEY (organization_id, clinic_id)
    REFERENCES clinic_app.identity_clinic (organization_id, id)
    NOT DEFERRABLE;
ALTER TABLE clinic_app.scheduling_appointment
    ADD CONSTRAINT scheduling_appointment_enrollment_fk
    FOREIGN KEY (organization_id, clinic_id, patient_id)
    REFERENCES clinic_app.intake_patientclinicenrollment
        (organization_id, clinic_id, patient_id)
    NOT DEFERRABLE;
ALTER TABLE clinic_app.scheduling_appointment
    ADD CONSTRAINT scheduling_appointment_positive_minute_range_check
    CHECK (
        end_at > start_at
        AND pg_catalog.date_trunc('minute', start_at) = start_at
        AND pg_catalog.date_trunc('minute', end_at) = end_at
    );
ALTER TABLE clinic_app.scheduling_appointment
    ADD CONSTRAINT scheduling_appointment_fingerprint_32_check
    CHECK (pg_catalog.octet_length(create_fingerprint) = 32);
ALTER TABLE clinic_app.scheduling_appointment
    ADD CONSTRAINT scheduling_appointment_lifecycle_check
    CHECK (
        (status = 'scheduled'
            AND cancellation_reason IS NULL
            AND cancelled_at IS NULL)
        OR
        (status = 'cancelled'
            AND cancellation_reason IN (
                'patient_request', 'clinic_request',
                'practitioner_unavailable', 'duplicate', 'other'
            )
            AND cancelled_at IS NOT NULL)
    );
CREATE FUNCTION clinic_app.scheduling_appointment_guard_v1()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, clinic_app
AS $function$
DECLARE
    clinic_timezone text;
BEGIN
    IF TG_OP = 'UPDATE' AND OLD.status = 'cancelled' AND ROW(
        NEW.start_at, NEW.end_at, NEW.status,
        NEW.cancellation_reason, NEW.cancelled_at
    ) IS DISTINCT FROM ROW(
        OLD.start_at, OLD.end_at, OLD.status,
        OLD.cancellation_reason, OLD.cancelled_at
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            CONSTRAINT = 'scheduling_appointment_terminal_check',
            MESSAGE = 'cancelled appointment is terminal';
    END IF;
    IF NEW.status = 'scheduled' THEN
        IF NEW.start_at <= CURRENT_TIMESTAMP THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                CONSTRAINT = 'scheduling_appointment_future_check',
                MESSAGE = 'scheduled appointment must be future';
        END IF;
        SELECT clinic.timezone
          INTO clinic_timezone
          FROM clinic_app.identity_clinic AS clinic
         WHERE clinic.organization_id = NEW.organization_id
           AND clinic.id = NEW.clinic_id;
        IF clinic_timezone IS NULL OR
           (NEW.start_at AT TIME ZONE clinic_timezone)::date <>
           (NEW.end_at AT TIME ZONE clinic_timezone)::date THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                CONSTRAINT = 'scheduling_appointment_local_day_check',
                MESSAGE = 'appointment must fit one clinic-local date';
        END IF;
        IF NOT EXISTS (
            SELECT 1
              FROM clinic_app.scheduling_availabilityblock AS availability
             WHERE availability.organization_id = NEW.organization_id
               AND availability.clinic_id = NEW.clinic_id
               AND availability.practitioner_id = NEW.practitioner_id
               AND availability.retired_at IS NULL
               AND availability.start_at <= NEW.start_at
               AND availability.end_at >= NEW.end_at
        ) THEN
            RAISE EXCEPTION USING
                ERRCODE = '23514',
                CONSTRAINT = 'scheduling_appointment_active_availability_check',
                MESSAGE = 'appointment lacks covering active availability';
        END IF;
    END IF;
    RETURN NEW;
END;
$function$;
REVOKE ALL ON FUNCTION clinic_app.scheduling_appointment_guard_v1()
    FROM PUBLIC, clinic_app, clinic_resolver;
CREATE TRIGGER scheduling_appointment_guard
BEFORE INSERT OR UPDATE ON clinic_app.scheduling_appointment
FOR EACH ROW EXECUTE FUNCTION clinic_app.scheduling_appointment_guard_v1();

CREATE FUNCTION clinic_app.scheduling_appointment_reject_delete_v1()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, clinic_app
AS $function$
BEGIN
    RAISE EXCEPTION USING
        ERRCODE = '23514',
        CONSTRAINT = 'scheduling_appointment_no_delete_check',
        MESSAGE = 'appointment hard delete is forbidden';
END;
$function$;
REVOKE ALL ON FUNCTION clinic_app.scheduling_appointment_reject_delete_v1()
    FROM PUBLIC, clinic_app, clinic_resolver;
CREATE TRIGGER scheduling_appointment_no_delete
BEFORE DELETE ON clinic_app.scheduling_appointment
FOR EACH ROW EXECUTE FUNCTION clinic_app.scheduling_appointment_reject_delete_v1();
"""

REVERSE_INTEGRITY_SQL = """
DROP TRIGGER IF EXISTS scheduling_appointment_no_delete
    ON clinic_app.scheduling_appointment;
DROP FUNCTION IF EXISTS clinic_app.scheduling_appointment_reject_delete_v1();
DROP TRIGGER IF EXISTS scheduling_appointment_guard
    ON clinic_app.scheduling_appointment;
DROP FUNCTION IF EXISTS clinic_app.scheduling_appointment_guard_v1();
ALTER TABLE clinic_app.scheduling_appointment
    DROP CONSTRAINT scheduling_appointment_lifecycle_check,
    DROP CONSTRAINT scheduling_appointment_fingerprint_32_check,
    DROP CONSTRAINT scheduling_appointment_positive_minute_range_check,
    DROP CONSTRAINT scheduling_appointment_enrollment_fk,
    DROP CONSTRAINT scheduling_appointment_org_clinic_fk;
"""

RUNTIME_ACL_SQL = """
REVOKE ALL PRIVILEGES
    ON TABLE clinic_app.scheduling_appointment
    FROM PUBLIC, clinic_app, clinic_resolver;
REVOKE ALL PRIVILEGES (
    id, organization_id, clinic_id, patient_id, practitioner_id,
    start_at, end_at, idempotency_key, create_fingerprint, status,
    cancellation_reason, cancelled_at, created_at, updated_at
)
    ON TABLE clinic_app.scheduling_appointment
    FROM PUBLIC, clinic_app, clinic_resolver;
GRANT SELECT, INSERT
    ON TABLE clinic_app.scheduling_appointment
    TO clinic_app;
GRANT UPDATE (
    start_at, end_at, status, cancellation_reason, cancelled_at, updated_at
)
    ON TABLE clinic_app.scheduling_appointment
    TO clinic_app;
"""

REVERSE_RUNTIME_ACL_SQL = """
REVOKE ALL PRIVILEGES
    ON TABLE clinic_app.scheduling_appointment
    FROM clinic_app;
REVOKE ALL PRIVILEGES (
    start_at, end_at, status, cancellation_reason, cancelled_at, updated_at
)
    ON TABLE clinic_app.scheduling_appointment
    FROM clinic_app;
"""

INDEX_SQL = """
CREATE INDEX scheduling_appointment_clinic_start_idx
    ON clinic_app.scheduling_appointment (clinic_id, start_at);
CREATE INDEX scheduling_appointment_practitioner_start_idx
    ON clinic_app.scheduling_appointment (practitioner_id, start_at);
CREATE INDEX scheduling_appointment_patient_start_idx
    ON clinic_app.scheduling_appointment (patient_id, start_at);
CREATE INDEX scheduling_appointment_status_start_idx
    ON clinic_app.scheduling_appointment (status, start_at);
"""

REVERSE_INDEX_SQL = """
DROP INDEX IF EXISTS clinic_app.scheduling_appointment_status_start_idx;
DROP INDEX IF EXISTS clinic_app.scheduling_appointment_patient_start_idx;
DROP INDEX IF EXISTS clinic_app.scheduling_appointment_practitioner_start_idx;
DROP INDEX IF EXISTS clinic_app.scheduling_appointment_clinic_start_idx;
"""
