"""Frozen SQL for the retention authority migration."""

from ._principal_sql import SQL as PRINCIPAL_SQL

_start = PRINCIPAL_SQL.index("CREATE FUNCTION clinic_app.principal_scope(")
_end = PRINCIPAL_SQL.index("$f$;", _start) + len("$f$;")
SQL = (
    "SET LOCAL ROLE clinic_resolver;\n"
    + PRINCIPAL_SQL[_start:_end]
    .replace("clinic_app.principal_scope", "clinic_app.retention_principal_scope")
    .replace("p.grant_set_version=1", "p.grant_set_version=3 AND p.purpose='retention'")
    .replace(
        "g.permission='appointment.read'",
        "g.permission='retention.execute' AND g.grant_set_version=3",
    )
    + """
REVOKE ALL ON FUNCTION clinic_app.retention_principal_scope(uuid,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.retention_principal_scope(uuid,uuid)
 TO clinic_agent;
RESET ROLE;
"""
)
