"""Lifecycle trigger and grant SQL for the provider capability registry.

The state machine lives in the database so no application path — ORM,
raw SQL or a future adapter — can move a version outside it. Illegal
transitions raise SQLSTATE P0001 with message ``provider_transition_denied``;
hard deletes and immutable-field edits raise the shared 23514 integrity
error used across the codebase.
"""

from typing import Final

TRANSITION_SQL: Final = """
CREATE FUNCTION clinic_app.providers_version_transition_v1()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, clinic_app
AS $function$
BEGIN
    IF NEW.capability_id IS DISTINCT FROM OLD.capability_id
        OR NEW.provider IS DISTINCT FROM OLD.provider
        OR NEW.account IS DISTINCT FROM OLD.account
        OR NEW.environment IS DISTINCT FROM OLD.environment
        OR NEW.api_version IS DISTINCT FROM OLD.api_version
        OR NEW.region IS DISTINCT FROM OLD.region
        OR NEW.retention_terms IS DISTINCT FROM OLD.retention_terms
        OR NEW.created_at IS DISTINCT FROM OLD.created_at
    THEN
        RAISE EXCEPTION 'provider version fields are immutable'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.state IS NOT DISTINCT FROM OLD.state
    THEN
        RAISE EXCEPTION 'provider_transition_denied'
            USING ERRCODE = 'P0001';
    END IF;
    -- An approval may be bound exactly when the transition enters the
    -- first gated state (approved_to_test) or the terminal revoked state,
    -- so an owner can revoke an unapproved candidate without first
    -- approving it. Once bound, the approval can never change.
    IF NEW.approval_id IS DISTINCT FROM OLD.approval_id
        AND NOT (
            OLD.approval_id IS NULL
            AND NEW.state IN ('approved_to_test', 'revoked')
        )
    THEN
        RAISE EXCEPTION 'provider_transition_denied'
            USING ERRCODE = 'P0001';
    END IF;
    -- The bound approval must belong to the same capability; a foreign
    -- approval can never govern this key's lifecycle.
    IF NEW.approval_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM clinic_app.providers_capabilityapproval a
        WHERE a.id = NEW.approval_id AND a.capability_id = NEW.capability_id
    )
    THEN
        RAISE EXCEPTION 'provider capability binding violated'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.state NOT IN ('researched', 'selected_in_plan')
        AND NEW.approval_id IS NULL
    THEN
        RAISE EXCEPTION 'provider_transition_denied'
            USING ERRCODE = 'P0001';
    END IF;
    IF NOT (
            (OLD.state = 'researched' AND NEW.state = 'selected_in_plan')
            OR (OLD.state = 'selected_in_plan' AND NEW.state = 'approved_to_test')
            OR (OLD.state = 'approved_to_test' AND NEW.state = 'sandbox')
            OR (OLD.state = 'sandbox' AND NEW.state = 'production_authorized')
            OR (OLD.state = 'production_authorized' AND NEW.state = 'activated')
            OR (OLD.state = 'activated' AND NEW.state = 'degraded')
            OR (OLD.state = 'degraded' AND NEW.state = 'activated')
            OR (OLD.state <> 'revoked' AND NEW.state = 'revoked')
        )
    THEN
        RAISE EXCEPTION 'provider_transition_denied'
            USING ERRCODE = 'P0001';
    END IF;
    RETURN NEW;
END;
$function$;
REVOKE ALL ON FUNCTION clinic_app.providers_version_transition_v1()
    FROM PUBLIC, clinic_app, clinic_resolver;
CREATE TRIGGER providers_version_transition
BEFORE UPDATE ON clinic_app.providers_capabilityversion
FOR EACH ROW EXECUTE FUNCTION clinic_app.providers_version_transition_v1();

CREATE FUNCTION clinic_app.providers_version_insert_v1()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, clinic_app
AS $function$
BEGIN
    IF NEW.state NOT IN ('researched', 'selected_in_plan')
        OR NEW.approval_id IS NOT NULL
    THEN
        RAISE EXCEPTION 'provider_transition_denied'
            USING ERRCODE = 'P0001';
    END IF;
    RETURN NEW;
END;
$function$;
REVOKE ALL ON FUNCTION clinic_app.providers_version_insert_v1()
    FROM PUBLIC, clinic_app, clinic_resolver;
CREATE TRIGGER providers_version_insert
BEFORE INSERT ON clinic_app.providers_capabilityversion
FOR EACH ROW EXECUTE FUNCTION clinic_app.providers_version_insert_v1();

CREATE FUNCTION clinic_app.providers_capability_guard_v1()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, clinic_app
AS $function$
BEGIN
    IF NEW.key IS DISTINCT FROM OLD.key
        OR NEW.clinic_id IS DISTINCT FROM OLD.clinic_id
        OR NEW.record_ref IS DISTINCT FROM OLD.record_ref
        OR NEW.description IS DISTINCT FROM OLD.description
        OR NEW.created_at IS DISTINCT FROM OLD.created_at
    THEN
        RAISE EXCEPTION 'provider capability fields are immutable'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$function$;
REVOKE ALL ON FUNCTION clinic_app.providers_capability_guard_v1()
    FROM PUBLIC, clinic_app, clinic_resolver;
CREATE TRIGGER providers_capability_guard
BEFORE UPDATE ON clinic_app.providers_providercapability
FOR EACH ROW EXECUTE FUNCTION clinic_app.providers_capability_guard_v1();

CREATE FUNCTION clinic_app.providers_capability_binding_v1()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, clinic_app
AS $function$
BEGIN
    IF NEW.current_version_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM clinic_app.providers_capabilityversion v
        WHERE v.id = NEW.current_version_id AND v.capability_id = NEW.id
    )
    THEN
        RAISE EXCEPTION 'provider capability binding violated'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$function$;
REVOKE ALL ON FUNCTION clinic_app.providers_capability_binding_v1()
    FROM PUBLIC, clinic_app, clinic_resolver;
CREATE TRIGGER providers_capability_binding
BEFORE INSERT OR UPDATE ON clinic_app.providers_providercapability
FOR EACH ROW EXECUTE FUNCTION clinic_app.providers_capability_binding_v1();

CREATE FUNCTION clinic_app.providers_history_binding_v1()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, clinic_app
AS $function$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM clinic_app.providers_capabilityversion v
        WHERE v.id = NEW.version_id AND v.capability_id = NEW.capability_id
    )
    THEN
        RAISE EXCEPTION 'provider capability binding violated'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.approval_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM clinic_app.providers_capabilityapproval a
        WHERE a.id = NEW.approval_id AND a.capability_id = NEW.capability_id
    )
    THEN
        RAISE EXCEPTION 'provider capability binding violated'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$function$;
REVOKE ALL ON FUNCTION clinic_app.providers_history_binding_v1()
    FROM PUBLIC, clinic_app, clinic_resolver;
CREATE TRIGGER providers_activation_binding
BEFORE INSERT ON clinic_app.providers_activationrecord
FOR EACH ROW EXECUTE FUNCTION clinic_app.providers_history_binding_v1();
CREATE TRIGGER providers_healthevent_binding
BEFORE INSERT ON clinic_app.providers_healthevent
FOR EACH ROW EXECUTE FUNCTION clinic_app.providers_history_binding_v1();

CREATE FUNCTION clinic_app.providers_reject_delete_v1()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, clinic_app
AS $function$
BEGIN
    RAISE EXCEPTION 'provider registry records are never hard deleted'
        USING ERRCODE = '23514';
END;
$function$;
REVOKE ALL ON FUNCTION clinic_app.providers_reject_delete_v1()
    FROM PUBLIC, clinic_app, clinic_resolver;
CREATE TRIGGER providers_capability_no_delete
BEFORE DELETE ON clinic_app.providers_providercapability
FOR EACH ROW EXECUTE FUNCTION clinic_app.providers_reject_delete_v1();
CREATE TRIGGER providers_version_no_delete
BEFORE DELETE ON clinic_app.providers_capabilityversion
FOR EACH ROW EXECUTE FUNCTION clinic_app.providers_reject_delete_v1();
CREATE TRIGGER providers_approval_no_delete
BEFORE DELETE ON clinic_app.providers_capabilityapproval
FOR EACH ROW EXECUTE FUNCTION clinic_app.providers_reject_delete_v1();
CREATE TRIGGER providers_activation_no_delete
BEFORE DELETE ON clinic_app.providers_activationrecord
FOR EACH ROW EXECUTE FUNCTION clinic_app.providers_reject_delete_v1();
CREATE TRIGGER providers_healthevent_no_delete
BEFORE DELETE ON clinic_app.providers_healthevent
FOR EACH ROW EXECUTE FUNCTION clinic_app.providers_reject_delete_v1();

CREATE FUNCTION clinic_app.providers_immutable_row_v1()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, clinic_app
AS $function$
BEGIN
    RAISE EXCEPTION 'provider registry record is immutable'
        USING ERRCODE = '23514';
END;
$function$;
REVOKE ALL ON FUNCTION clinic_app.providers_immutable_row_v1()
    FROM PUBLIC, clinic_app, clinic_resolver;
CREATE TRIGGER providers_approval_immutable
BEFORE UPDATE ON clinic_app.providers_capabilityapproval
FOR EACH ROW EXECUTE FUNCTION clinic_app.providers_immutable_row_v1();
CREATE TRIGGER providers_activation_immutable
BEFORE UPDATE ON clinic_app.providers_activationrecord
FOR EACH ROW EXECUTE FUNCTION clinic_app.providers_immutable_row_v1();
CREATE TRIGGER providers_healthevent_immutable
BEFORE UPDATE ON clinic_app.providers_healthevent
FOR EACH ROW EXECUTE FUNCTION clinic_app.providers_immutable_row_v1();
"""

REVERSE_TRANSITION_SQL: Final = """
DROP TRIGGER IF EXISTS providers_healthevent_binding
    ON clinic_app.providers_healthevent;
DROP TRIGGER IF EXISTS providers_activation_binding
    ON clinic_app.providers_activationrecord;
DROP TRIGGER IF EXISTS providers_capability_binding
    ON clinic_app.providers_providercapability;
DROP FUNCTION IF EXISTS clinic_app.providers_history_binding_v1();
DROP FUNCTION IF EXISTS clinic_app.providers_capability_binding_v1();
DROP TRIGGER IF EXISTS providers_healthevent_immutable
    ON clinic_app.providers_healthevent;
DROP TRIGGER IF EXISTS providers_activation_immutable
    ON clinic_app.providers_activationrecord;
DROP TRIGGER IF EXISTS providers_approval_immutable
    ON clinic_app.providers_capabilityapproval;
DROP FUNCTION IF EXISTS clinic_app.providers_immutable_row_v1();
DROP TRIGGER IF EXISTS providers_healthevent_no_delete
    ON clinic_app.providers_healthevent;
DROP TRIGGER IF EXISTS providers_activation_no_delete
    ON clinic_app.providers_activationrecord;
DROP TRIGGER IF EXISTS providers_approval_no_delete
    ON clinic_app.providers_capabilityapproval;
DROP TRIGGER IF EXISTS providers_version_no_delete
    ON clinic_app.providers_capabilityversion;
DROP TRIGGER IF EXISTS providers_capability_no_delete
    ON clinic_app.providers_providercapability;
DROP FUNCTION IF EXISTS clinic_app.providers_reject_delete_v1();
DROP TRIGGER IF EXISTS providers_capability_guard
    ON clinic_app.providers_providercapability;
DROP FUNCTION IF EXISTS clinic_app.providers_capability_guard_v1();
DROP TRIGGER IF EXISTS providers_version_insert
    ON clinic_app.providers_capabilityversion;
DROP FUNCTION IF EXISTS clinic_app.providers_version_insert_v1();
DROP TRIGGER IF EXISTS providers_version_transition
    ON clinic_app.providers_capabilityversion;
DROP FUNCTION IF EXISTS clinic_app.providers_version_transition_v1();
"""

# Runtime ACL trimming is emitted per table through
# ``apps.tenancy.migrations_support.revoke_default_dml`` in the migration.
PROVIDER_TABLES: Final = (
    "providers_providercapability",
    "providers_capabilityversion",
    "providers_capabilityapproval",
    "providers_activationrecord",
    "providers_healthevent",
)

REVERSE_RUNTIME_ACL_SQL: Final = "".join(
    f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE clinic_app.{table} TO clinic_app;\n"
    for table in PROVIDER_TABLES
)
