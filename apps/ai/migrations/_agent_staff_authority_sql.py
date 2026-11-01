"""Agent queue staff authority follows the RP automation row, not task assignment."""

# The released body (``_agent_access_sql``) also admitted ``tasks.assign``, which
# every v2 workflow bundle holds. Keep owner, SECURITY DEFINER and search_path.
SQL = """SET LOCAL ROLE clinic_resolver;
CREATE OR REPLACE FUNCTION clinic_app.agent_staff(clinic uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT clinic_app.has_permission('automation.admin',clinic,NULL)
 OR clinic_app.has_permission('automation.organization',clinic,NULL)
$f$;
RESET ROLE;
"""

# The prior body, byte-for-byte equivalent to the released definition.
REVERSE_SQL = """SET LOCAL ROLE clinic_resolver;
CREATE OR REPLACE FUNCTION clinic_app.agent_staff(clinic uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT clinic_app.has_permission('automation.admin',clinic,NULL)
 OR clinic_app.has_permission('automation.organization',clinic,NULL)
 OR clinic_app.has_permission('tasks.assign',clinic,NULL)
$f$;
RESET ROLE;
"""
