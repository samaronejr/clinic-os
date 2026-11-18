"""Masked support overview: aggregate counts only, behind the verified support grant.

The enterprise fence hides every generic tenant row from a support context, so
support diagnostics read queue, task and staffing health only through this
resolver. It re-runs ``enterprise_support_scope`` with the caller's session hash
and also requires the grant the service installed in
``app.current_support_grant``. It returns closed-vocabulary labels and counts;
no identifier, name, subject reference, payload, error text or clinical row ever
leaves it.

The reverse follows 0051's rule: it refuses before any change while an
enterprise row exists (populated authority history rolls back by restore), so
a refused 0051 rollback never strips this resolver first.
"""

from apps.identity.enterprise_contract import TABLES

SQL = """
CREATE FUNCTION clinic_app.enterprise_support_overview(
 requested_grant uuid, requested_clinic uuid, requested_session bytea)
RETURNS TABLE(signal text, channel text, state text, item_count bigint,
 oldest_at timestamp with time zone)
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,pg_temp AS $f$
 WITH scope AS (
 SELECT g.organization_id, g.clinic_id
 FROM clinic_app.enterprise_support_scope(
 requested_grant, requested_clinic, requested_session) verified
 JOIN clinic_app.identity_supportaccessgrant g ON g.id=requested_grant
 AND g.clinic_id=requested_clinic AND g.organization_id=verified.organization_id
 WHERE NULLIF(current_setting('app.current_support_grant',true),'')
 =requested_grant::text)
 SELECT 'outbox'::text, o.channel::text, o.status::text, count(*)::bigint,
 min(o.created_at)
 FROM scope JOIN clinic_app.comms_integrationoperation o
 ON o.organization_id=scope.organization_id AND o.clinic_id=scope.clinic_id
 GROUP BY o.channel, o.status
 UNION ALL
 SELECT 'task'::text, ''::text, t.state::text, count(*)::bigint, min(t.due_at)
 FROM scope JOIN clinic_app.workflows_task t
 ON t.organization_id=scope.organization_id AND t.clinic_id=scope.clinic_id
 GROUP BY t.state
 UNION ALL
 SELECT 'units'::text, ''::text, ''::text, count(*)::bigint,
 NULL::timestamp with time zone
 FROM scope JOIN clinic_app.identity_clinic unit
 ON unit.organization_id=scope.organization_id
 GROUP BY scope.organization_id
 UNION ALL
 SELECT 'staff'::text, ''::text, membership.role::text,
 count(DISTINCT membership.user_id)::bigint, NULL::timestamp with time zone
 FROM scope JOIN clinic_app.identity_userclinicrole membership
 ON membership.organization_id=scope.organization_id
 AND membership.clinic_id=scope.clinic_id AND membership.revoked_at IS NULL
 GROUP BY membership.role
$f$;
ALTER FUNCTION clinic_app.enterprise_support_overview(uuid,uuid,bytea)
 OWNER TO clinic_resolver;
SET LOCAL ROLE clinic_resolver;
REVOKE ALL ON FUNCTION clinic_app.enterprise_support_overview(uuid,uuid,bytea)
 FROM PUBLIC,clinic_app,clinic_agent;
GRANT EXECUTE ON FUNCTION clinic_app.enterprise_support_overview(uuid,uuid,bytea)
 TO clinic_app;
RESET ROLE;
"""

_populated = "\n OR ".join(
    "EXISTS (SELECT 1 FROM clinic_app.__TABLE__)".replace("__TABLE__", table)
    for table in TABLES
)
REVERSE_SQL = """
SET LOCAL ROLE clinic_resolver;
DO $populated$
BEGIN
 IF __POPULATED__ THEN
 RAISE EXCEPTION 'populated enterprise identity rolls back only by restore'
 USING ERRCODE='55000';
 END IF;
END $populated$;
DROP FUNCTION clinic_app.enterprise_support_overview(uuid,uuid,bytea);
RESET ROLE;
""".replace("__POPULATED__", _populated)
