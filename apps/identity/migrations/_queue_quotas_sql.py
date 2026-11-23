"""Frozen SQL for the queue quotas migration."""

RESOLVER_SQL = """
SET LOCAL ROLE clinic_resolver;

CREATE FUNCTION clinic_app.identity_queue_quotas(
    requested_org pg_catalog.uuid
)
RETURNS pg_catalog.jsonb
LANGUAGE sql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp
AS $function$
    SELECT configuration.queue_quotas
    FROM clinic_app.identity_clinicconfiguration AS configuration
    WHERE configuration.organization_id = requested_org
    ORDER BY configuration.created_at DESC, configuration.id DESC
    LIMIT 1
$function$;

REVOKE ALL PRIVILEGES
    ON FUNCTION clinic_app.identity_queue_quotas(pg_catalog.uuid)
    FROM PUBLIC;
GRANT EXECUTE
    ON FUNCTION clinic_app.identity_queue_quotas(pg_catalog.uuid)
    TO clinic_app;

RESET ROLE;
"""

REVERSE_RESOLVER_SQL = """
SET LOCAL ROLE clinic_resolver;
DROP FUNCTION IF EXISTS clinic_app.identity_queue_quotas(pg_catalog.uuid);
RESET ROLE;
"""
