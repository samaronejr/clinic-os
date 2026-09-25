"""Aggregate outbox state counts for the sessionless metrics scrape.

``/internal/metrics`` runs without a tenant context, so RLS hides every
``comms_integrationoperation`` row from ``clinic_app``. This resolver returns
only per-status aggregates — counts and oldest row age — never row content,
subject ids or provider references, so the scrape can report outbox SLIs
without widening table grants.
"""

SQL = """
SET LOCAL ROLE clinic_resolver;

-- Aggregate-only view of the outbox: status, count and oldest row per
-- status. No tenant key, subject reference or payload ever leaves the
-- function, so the sessionless metrics endpoint can expose backlog SLIs.
CREATE FUNCTION clinic_app.comms_operation_state_counts_v1()
RETURNS TABLE(
    status text,
    operation_count bigint,
    oldest_created_at timestamp with time zone
)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
 SELECT operation.status::text,
        count(*)::bigint,
        min(operation.created_at)
   FROM clinic_app.comms_integrationoperation AS operation
  GROUP BY operation.status
$f$;
REVOKE ALL ON FUNCTION clinic_app.comms_operation_state_counts_v1() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.comms_operation_state_counts_v1()
    TO clinic_app;
RESET ROLE;
"""

REVERSE_SQL = """
SET LOCAL ROLE clinic_resolver;
DROP FUNCTION IF EXISTS clinic_app.comms_operation_state_counts_v1();
RESET ROLE;
"""
