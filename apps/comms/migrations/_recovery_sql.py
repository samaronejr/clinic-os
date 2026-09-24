"""Bounded pending-operation recovery scan shared by the reaper task."""

SQL = """
SET LOCAL ROLE clinic_resolver;

-- Worker enumeration discloses only opaque due job IDs, never destinations.
-- Unlike comms_due_reminders_v1 this covers every subject type: an
-- on-commit dispatch lost to a broker outage leaves the row pending
-- forever unless something rescans undispatched work. ``not_before`` is
-- NULL for enqueue_operation rows, so the due predicate must admit NULL.
CREATE FUNCTION clinic_app.comms_recover_pending_v1()
RETURNS SETOF uuid LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
 SELECT id FROM clinic_app.comms_integrationoperation
 WHERE status IN ('pending', 'in_progress')
   AND (not_before IS NULL OR not_before <= statement_timestamp())
   AND ((status='pending' AND last_error='')
        OR updated_at <= statement_timestamp()-interval '1 minute')
 ORDER BY created_at, id LIMIT 100
$f$;
REVOKE ALL ON FUNCTION clinic_app.comms_recover_pending_v1() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.comms_recover_pending_v1() TO clinic_app;
RESET ROLE;
"""

REVERSE_SQL = """
SET LOCAL ROLE clinic_resolver;
DROP FUNCTION IF EXISTS clinic_app.comms_recover_pending_v1();
RESET ROLE;
"""
