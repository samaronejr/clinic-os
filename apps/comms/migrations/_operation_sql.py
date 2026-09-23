"""Frozen SQL boundaries for the integration-operation schema migration."""

INTEGRITY_SQL = """
ALTER TABLE clinic_app.comms_integrationoperation
    ADD CONSTRAINT comms_operation_org_clinic_fk
    FOREIGN KEY (organization_id, clinic_id)
    REFERENCES clinic_app.identity_clinic (organization_id, id)
    NOT DEFERRABLE;

CREATE FUNCTION clinic_app.comms_operation_terminal_guard_v1()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, clinic_app
AS $function$
BEGIN
    IF TG_OP = 'UPDATE' AND OLD.status IN ('delivered', 'failed', 'cancelled')
        AND ROW(
            NEW.organization_id, NEW.clinic_id, NEW.actor_id,
            NEW.channel, NEW.provider, NEW.subject_type, NEW.subject_id,
            NEW.status, NEW.attempt_count, NEW.max_attempts,
            NEW.provider_reference, NEW.last_callback_event_id,
            NEW.last_error, NEW.idempotency_key, NEW.created_at
        ) IS DISTINCT FROM ROW(
            OLD.organization_id, OLD.clinic_id, OLD.actor_id,
            OLD.channel, OLD.provider, OLD.subject_type, OLD.subject_id,
            OLD.status, OLD.attempt_count, OLD.max_attempts,
            OLD.provider_reference, OLD.last_callback_event_id,
            OLD.last_error, OLD.idempotency_key, OLD.created_at
        )
    THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            CONSTRAINT = 'comms_operation_terminal_check',
            MESSAGE = 'terminal integration operation is immutable';
    END IF;
    RETURN NEW;
END;
$function$;
REVOKE ALL ON FUNCTION clinic_app.comms_operation_terminal_guard_v1()
    FROM PUBLIC, clinic_app, clinic_resolver;
CREATE TRIGGER comms_operation_terminal_guard
BEFORE UPDATE ON clinic_app.comms_integrationoperation
FOR EACH ROW EXECUTE FUNCTION clinic_app.comms_operation_terminal_guard_v1();

CREATE FUNCTION clinic_app.comms_operation_reject_delete_v1()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, clinic_app
AS $function$
BEGIN
    RAISE EXCEPTION USING
        ERRCODE = '23514',
        CONSTRAINT = 'comms_operation_no_delete_check',
        MESSAGE = 'integration operation hard delete is forbidden';
END;
$function$;
REVOKE ALL ON FUNCTION clinic_app.comms_operation_reject_delete_v1()
    FROM PUBLIC, clinic_app, clinic_resolver;
CREATE TRIGGER comms_operation_no_delete
BEFORE DELETE ON clinic_app.comms_integrationoperation
FOR EACH ROW EXECUTE FUNCTION clinic_app.comms_operation_reject_delete_v1();
"""

REVERSE_INTEGRITY_SQL = """
DROP TRIGGER IF EXISTS comms_operation_no_delete
    ON clinic_app.comms_integrationoperation;
DROP FUNCTION IF EXISTS clinic_app.comms_operation_reject_delete_v1();
DROP TRIGGER IF EXISTS comms_operation_terminal_guard
    ON clinic_app.comms_integrationoperation;
DROP FUNCTION IF EXISTS clinic_app.comms_operation_terminal_guard_v1();
ALTER TABLE clinic_app.comms_integrationoperation
    DROP CONSTRAINT comms_operation_org_clinic_fk;
"""

RESOLVER_SQL = """
SET LOCAL ROLE clinic_resolver;

CREATE FUNCTION clinic_app.comms_operation_scope(
    requested_operation pg_catalog.uuid
)
RETURNS TABLE(
    organization_id pg_catalog.uuid,
    clinic_id pg_catalog.uuid,
    actor_id pg_catalog.uuid
)
LANGUAGE sql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp
AS $function$
    SELECT operation.organization_id,
           operation.clinic_id,
           operation.actor_id
    FROM clinic_app.comms_integrationoperation AS operation
    WHERE operation.id = requested_operation
$function$;

CREATE FUNCTION clinic_app.comms_operation_callback_scope(
    requested_provider pg_catalog.text,
    requested_reference pg_catalog.text
)
RETURNS TABLE(
    operation_id pg_catalog.uuid,
    organization_id pg_catalog.uuid,
    clinic_id pg_catalog.uuid,
    actor_id pg_catalog.uuid
)
LANGUAGE sql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp
AS $function$
    SELECT operation.id,
           operation.organization_id,
           operation.clinic_id,
           operation.actor_id
    FROM clinic_app.comms_integrationoperation AS operation
    WHERE operation.provider = requested_provider
      AND operation.provider_reference = requested_reference
$function$;

REVOKE ALL PRIVILEGES
    ON FUNCTION clinic_app.comms_operation_scope(pg_catalog.uuid)
    FROM PUBLIC;
REVOKE ALL PRIVILEGES
    ON FUNCTION clinic_app.comms_operation_callback_scope(
        pg_catalog.text, pg_catalog.text
    )
    FROM PUBLIC;

GRANT EXECUTE
    ON FUNCTION clinic_app.comms_operation_scope(pg_catalog.uuid)
    TO clinic_app;
GRANT EXECUTE
    ON FUNCTION clinic_app.comms_operation_callback_scope(
        pg_catalog.text, pg_catalog.text
    )
    TO clinic_app;

RESET ROLE;
"""

REVERSE_RESOLVER_SQL = """
SET LOCAL ROLE clinic_resolver;
DROP FUNCTION IF EXISTS clinic_app.comms_operation_scope(pg_catalog.uuid);
DROP FUNCTION IF EXISTS clinic_app.comms_operation_callback_scope(
    pg_catalog.text, pg_catalog.text
);
RESET ROLE;
"""

RUNTIME_ACL_SQL = """
REVOKE ALL PRIVILEGES
    ON TABLE clinic_app.comms_integrationoperation
    FROM PUBLIC, clinic_app, clinic_resolver;
REVOKE ALL PRIVILEGES (
    id, organization_id, clinic_id, actor_id, channel, provider,
    subject_type, subject_id, status, attempt_count, max_attempts,
    provider_reference, last_callback_event_id, last_error,
    idempotency_key, created_at, updated_at
)
    ON TABLE clinic_app.comms_integrationoperation
    FROM PUBLIC, clinic_app, clinic_resolver;
GRANT SELECT, INSERT
    ON TABLE clinic_app.comms_integrationoperation
    TO clinic_app;
GRANT UPDATE (
    status, attempt_count, provider_reference, last_callback_event_id,
    last_error, updated_at
)
    ON TABLE clinic_app.comms_integrationoperation
    TO clinic_app;
GRANT SELECT
    ON TABLE clinic_app.comms_integrationoperation
    TO clinic_resolver;
"""

REVERSE_RUNTIME_ACL_SQL = """
REVOKE ALL PRIVILEGES
    ON TABLE clinic_app.comms_integrationoperation
    FROM clinic_app, clinic_resolver;
REVOKE ALL PRIVILEGES (
    status, attempt_count, provider_reference, last_callback_event_id,
    last_error, updated_at
)
    ON TABLE clinic_app.comms_integrationoperation
    FROM clinic_app;
"""
