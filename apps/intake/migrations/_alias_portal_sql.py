"""Live portal marker and read-only retained consent lineage."""

from psycopg import sql

SQL = """
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.patient_alias_overview()
RETURNS timestamptz LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,pg_temp AS $function$
 SELECT max(a.unified_at)
 FROM clinic_app.current_patient_authority('enrollment_view') s
 CROSS JOIN LATERAL clinic_app.patient_alias_set(s.patient_id) a
$function$;
REVOKE ALL ON FUNCTION clinic_app.patient_alias_overview() FROM PUBLIC,clinic_app;
GRANT EXECUTE ON FUNCTION clinic_app.patient_alias_overview() TO clinic_app,
    clinic_owner;
RESET ROLE;
CREATE POLICY consent_alias_read ON clinic_app.consent_consentrevocation
FOR SELECT TO clinic_app USING (EXISTS (
    SELECT 1 FROM clinic_app.consent_consentacceptance a
    JOIN clinic_app.consent_session() s
      ON s.organization_id=a.organization_id AND s.clinic_id=a.clinic_id
    CROSS JOIN LATERAL clinic_app.patient_alias_set(s.patient_id) lineage
    WHERE a.id=acceptance_id AND
        a.organization_id=consent_consentrevocation.organization_id
      AND a.clinic_id=consent_consentrevocation.clinic_id
      AND a.patient_id=lineage.patient_id
));
"""

for _table in ("consentacceptance", "refusalrecord", "aiusedisclosure"):
    SQL += (
        sql.SQL("""
CREATE POLICY consent_alias_read ON clinic_app.{table}
FOR SELECT TO clinic_app USING (EXISTS (
    SELECT 1 FROM clinic_app.consent_session() s
    CROSS JOIN LATERAL clinic_app.patient_alias_set(s.patient_id) lineage
    WHERE s.organization_id={table}.organization_id
      AND s.clinic_id={table}.clinic_id
      AND {table}.patient_id=lineage.patient_id
));
""")
        .format(table=sql.Identifier("consent_" + _table))
        .as_string()
    )
