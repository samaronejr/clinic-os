"""A SCIM event names its scoped credential principal, never its human issuer."""

from apps.audit.migrations._v2_append_sql import INSTALL_V2_APPEND_SQL, TENANT_CONTEXT

_human_actor = """
    actor_setting := NULLIF(
        pg_catalog.current_setting('app.current_user_id', true), ''
    );
    IF actor_setting IS NULL OR NOT pg_catalog.pg_input_is_valid(
        actor_setting, 'uuid'
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'valid app.current_user_id is required';
    END IF;
    context_actor_user_id := actor_setting::pg_catalog.uuid;
"""
_directory_actor = """
    IF NULLIF(current_setting('app.current_scim_token',true),'') IS NOT NULL THEN
        IF NULLIF(current_setting('app.current_user_id',true),'') IS NOT NULL THEN
            RAISE EXCEPTION USING ERRCODE='22023',
                MESSAGE='mixed directory actor context is forbidden';
        END IF;
        SELECT scope.principal_id INTO context_actor_user_id
        FROM clinic_app.enterprise_scim_audit_scope(
            (payload->>'clinic_id')::uuid) scope
        WHERE scope.organization_id=context_organization_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE='22023',
                MESSAGE='valid directory actor context is required';
        END IF;
    ELSE
__HUMAN_ACTOR__
    END IF;
""".replace("__HUMAN_ACTOR__", _human_actor)
# Migration 0005 is the current append definition. Preserve all raw-string,
# schema, clinic, chain and payload checks and both existing signatures/ACLs.
# A new audit version must consciously carry this principal binding forward.
# The census decoder executes this module without str.count: split is the
# exactly-once check (tests/scheduling/clock_runsql.py).
if (
    len(TENANT_CONTEXT.split(_human_actor)) - 1 != 1
    or len(INSTALL_V2_APPEND_SQL.split(TENANT_CONTEXT)) - 1 != 1
):  # pragma: no cover - import-time contract
    message = "audit append actor binding moved"
    raise RuntimeError(message)
_tenant_context = TENANT_CONTEXT.replace(_human_actor, _directory_actor)
SQL = INSTALL_V2_APPEND_SQL.replace(TENANT_CONTEXT, _tenant_context).replace(
    "CREATE FUNCTION ", "CREATE OR REPLACE FUNCTION "
)
# The unused-rollback restores migration 0005's definitions byte for byte.
PRIOR_SQL = INSTALL_V2_APPEND_SQL.replace(
    "CREATE FUNCTION ", "CREATE OR REPLACE FUNCTION "
)
