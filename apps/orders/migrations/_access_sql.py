"""Closed order/result authority; lab proof never becomes staff authority."""

TENANT = "NULLIF(current_setting('app.current_tenant',true),'')::uuid"
ACTOR = "NULLIF(current_setting('app.current_user_id',true),'')::uuid"
TABLES = (
    "orders_labaccount",
    "orders_order",
    "orders_orderitem",
    "orders_orderevent",
    "orders_result",
    "orders_resultversion",
    "orders_resultobservation",
    "orders_resultmatch",
    "orders_resultquarantine",
    "orders_resultrejection",
    "orders_resultacknowledgment",
    "orders_resultrelease",
    "orders_resultworkitem",
)


def render(sql: str) -> str:
    """Substitute only migration-owned expressions."""
    return sql.replace("__TENANT__", TENANT).replace("__ACTOR__", ACTOR)


CURRENT_RESULT_READ_SQL = """
CREATE OR REPLACE FUNCTION clinic_app.orders_can_read_version(version uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT EXISTS(SELECT 1 FROM clinic_app.orders_resultversion v
 LEFT JOIN clinic_app.orders_resultmatch m ON m.version_id=v.id
 LEFT JOIN clinic_app.orders_order o ON o.id=coalesce(m.order_id,v.proposed_order_id)
 WHERE v.id=version AND v.organization_id=__TENANT__ AND (
 clinic_app.has_permission('order.route',v.clinic_id,NULL)
 OR ((m.id IS NOT NULL OR v.uploader_id=__ACTOR__)
 AND o.clinic_id=v.clinic_id AND o.organization_id=v.organization_id
 AND (m.id IS NULL OR (
 m.clinic_id=v.clinic_id AND m.organization_id=v.organization_id))
 AND clinic_app.has_permission('result.read',v.clinic_id,o.enrollment_id))))
$f$;
"""

CURRENT_INGEST_SCOPE_SQL = """
CREATE OR REPLACE FUNCTION clinic_app.orders_ingest_scope(
 clinic uuid, requested_order uuid, result uuid, predecessor uuid)
RETURNS TABLE(organization_id uuid,lab_account_id uuid,number integer)
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT __TENANT__,clinic_app.orders_lab_account(clinic),
 CASE WHEN predecessor IS NULL THEN 1 ELSE (
 SELECT v.number+1 FROM clinic_app.orders_resultversion v WHERE v.id=predecessor
 AND v.result_id=result AND v.clinic_id=clinic AND v.organization_id=__TENANT__
 AND coalesce((SELECT m.order_id FROM clinic_app.orders_resultmatch m
 WHERE m.version_id=v.id),v.proposed_order_id) IS NOT DISTINCT FROM requested_order
 AND (clinic_app.orders_lab_account(clinic) IS NULL
 OR v.lab_account_id=clinic_app.orders_lab_account(clinic))
 AND clinic_app.orders_is_current(v.id)) END
 WHERE clinic_app.orders_ingest_allowed(clinic,requested_order)
 AND ((result IS NULL AND predecessor IS NULL) OR EXISTS(
 SELECT 1 FROM clinic_app.orders_resultversion v WHERE v.id=predecessor
 AND v.result_id=result AND v.clinic_id=clinic AND v.organization_id=__TENANT__
 AND coalesce((SELECT m.order_id FROM clinic_app.orders_resultmatch m
 WHERE m.version_id=v.id),v.proposed_order_id) IS NOT DISTINCT FROM requested_order
 AND (clinic_app.orders_lab_account(clinic) IS NULL
 OR v.lab_account_id=clinic_app.orders_lab_account(clinic))
 AND clinic_app.orders_is_current(v.id)))
$f$;
"""

CURRENT_EVENT_RECEIPT_SQL = """
CREATE OR REPLACE FUNCTION clinic_app.orders_event_receipt(clinic uuid,event uuid)
RETURNS TABLE(result_id uuid,version_id uuid,number integer,state text,fingerprint text)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE v clinic_app.orders_resultversion; requested_order uuid;
BEGIN
 SELECT * INTO v FROM clinic_app.orders_resultversion r WHERE r.event_id=event
 AND r.clinic_id=clinic AND r.organization_id=__TENANT__;
 IF v.id IS NULL THEN RETURN; END IF;
 SELECT m.order_id INTO requested_order FROM clinic_app.orders_resultmatch m
 WHERE m.version_id=v.id;
 IF NOT clinic_app.orders_ingest_allowed(
 clinic,coalesce(requested_order,v.proposed_order_id))
 OR (clinic_app.orders_lab_account(clinic) IS NOT NULL
 AND v.lab_account_id IS DISTINCT FROM clinic_app.orders_lab_account(clinic)) THEN
 RAISE EXCEPTION 'result denied' USING ERRCODE='42501'; END IF;
 RETURN QUERY SELECT v.result_id,v.id,v.number,
 CASE WHEN EXISTS(SELECT 1 FROM clinic_app.orders_resultrelease r
 WHERE r.version_id=v.id) THEN 'released'
 WHEN EXISTS(SELECT 1 FROM clinic_app.orders_resultacknowledgment r
 WHERE r.version_id=v.id) THEN 'acknowledged'
 WHEN EXISTS(SELECT 1 FROM clinic_app.orders_resultmatch m
 WHERE m.version_id=v.id) THEN 'matched'
 WHEN EXISTS(SELECT 1 FROM clinic_app.orders_resultrejection r
 WHERE r.version_id=v.id) THEN 'rejected' ELSE 'quarantined' END,v.fingerprint::text;
END $f$;
"""


FUNCTIONS = """
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.orders_lab_account(clinic uuid)
RETURNS uuid LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT a.id FROM clinic_app.orders_labaccount a
 JOIN clinic_app.identity_serviceprincipal p ON p.id=a.principal_id
  AND p.clinic_id=a.clinic_id AND p.organization_id=a.organization_id
 WHERE a.clinic_id=clinic AND a.organization_id=__TENANT__
 AND a.active AND p.active AND p.purpose='lab_ingest'
 AND a.provider='synthetic-lab-v1'
 AND p.id=NULLIF(current_setting('app.current_principal',true),'')::uuid
 AND __ACTOR__ IS NULL
 AND NULLIF(current_setting('app.current_patient_session',true),'') IS NULL
 AND encode(clinic_app.digest(
   current_setting('app.orders_lab_proof',true),'sha256'),'hex')=a.signing_key_digest
$f$;
CREATE FUNCTION clinic_app.orders_resolve_lab(account uuid, proof text)
RETURNS TABLE(organization_id uuid,clinic_id uuid,principal_id uuid)
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT a.organization_id,a.clinic_id,a.principal_id
 FROM clinic_app.orders_labaccount a
 JOIN clinic_app.identity_serviceprincipal p ON p.id=a.principal_id
 AND p.organization_id=a.organization_id AND p.clinic_id=a.clinic_id
 WHERE a.id=account AND a.active AND p.active AND p.purpose='lab_ingest'
 AND a.provider='synthetic-lab-v1' AND proof~'^[0-9a-f]{64}$'
 AND a.signing_key_digest=encode(clinic_app.digest(proof,'sha256'),'hex')
 AND __ACTOR__ IS NULL
 AND NULLIF(current_setting('app.current_patient_session',true),'') IS NULL
 AND NULLIF(current_setting('app.current_principal',true),'') IS NULL
$f$;
CREATE FUNCTION clinic_app.orders_ingest_allowed(clinic uuid, requested_order uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT EXISTS(SELECT 1 FROM clinic_app.identity_clinic c
 WHERE c.id=clinic AND c.organization_id=__TENANT__) AND (
 clinic_app.orders_lab_account(clinic) IS NOT NULL AND
 (requested_order IS NULL OR EXISTS(SELECT 1 FROM clinic_app.orders_order o
 WHERE o.id=requested_order AND o.clinic_id=clinic AND o.organization_id=__TENANT__
 AND o.lab_account_id=clinic_app.orders_lab_account(clinic)))
 OR clinic_app.has_permission('order.route',clinic,NULL)
 OR EXISTS(SELECT 1 FROM clinic_app.orders_order o WHERE o.id=requested_order
 AND o.clinic_id=clinic AND o.organization_id=__TENANT__
 AND clinic_app.has_permission('order.place',clinic,o.enrollment_id)))
$f$;
CREATE FUNCTION clinic_app.orders_is_current(version uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT EXISTS(SELECT 1 FROM clinic_app.orders_resultversion v
 WHERE v.id=version AND v.organization_id=__TENANT__
 AND NOT EXISTS(SELECT 1 FROM clinic_app.orders_resultversion newer
 WHERE newer.result_id=v.result_id AND newer.number>v.number))
$f$;
"""
FUNCTIONS += CURRENT_RESULT_READ_SQL
FUNCTIONS += CURRENT_INGEST_SCOPE_SQL
FUNCTIONS += CURRENT_EVENT_RECEIPT_SQL
FUNCTIONS += """
CREATE FUNCTION clinic_app.orders_delegate_allowed(version uuid, grant_id uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT EXISTS(SELECT 1 FROM clinic_app.orders_resultmatch m
 JOIN clinic_app.orders_order o ON o.id=m.order_id
 JOIN clinic_app.identity_patientdelegategrant g ON
 g.represented_enrollment_id=o.enrollment_id AND g.clinic_id=o.clinic_id
 AND g.organization_id=o.organization_id
 WHERE m.version_id=version AND g.id=grant_id AND (
 g.organization_id=__TENANT__ OR EXISTS(
 SELECT 1 FROM clinic_app.current_patient_authority('records') a
 WHERE a.organization_id=g.organization_id AND a.clinic_id=g.clinic_id
 AND a.enrollment_id=g.represented_enrollment_id AND a.delegate_grant_id=g.id))
 AND g.state='active' AND g.revoked_at IS NULL
 AND g.expires_at>statement_timestamp()
 AND 'records'=ANY(g.operations) AND 'care_processing'=ANY(g.purposes))
$f$;
CREATE FUNCTION clinic_app.orders_correlated_order(account uuid,correlation uuid)
RETURNS uuid LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT o.id FROM clinic_app.orders_order o WHERE o.correlation=correlation
 AND o.lab_account_id=account AND o.organization_id=__TENANT__
 AND clinic_app.orders_lab_account(o.clinic_id)=account
$f$;
"""

PUBLIC_FUNCTIONS = (
    "orders_lab_account(uuid)",
    "orders_resolve_lab(uuid,text)",
    "orders_ingest_allowed(uuid,uuid)",
    "orders_is_current(uuid)",
    "orders_can_read_version(uuid)",
    "orders_ingest_scope(uuid,uuid,uuid,uuid)",
    "orders_event_receipt(uuid,uuid)",
    "orders_delegate_allowed(uuid,uuid)",
    "orders_correlated_order(uuid,uuid)",
)
FUNCTIONS += (
    "".join(
        f"REVOKE ALL ON FUNCTION clinic_app.{function} FROM PUBLIC;"
        f"GRANT EXECUTE ON FUNCTION clinic_app.{function} TO clinic_app;"
        for function in PUBLIC_FUNCTIONS
    )
    + "RESET ROLE;"
)

POSTURE = "".join(
    f"""
ALTER TABLE clinic_app.{table} ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.{table} FORCE ROW LEVEL SECURITY;
REVOKE ALL ON clinic_app.{table} FROM PUBLIC,clinic_app,clinic_agent;
CREATE POLICY setup_tenant ON clinic_app.{table} TO clinic_owner
 USING (organization_id=__TENANT__) WITH CHECK (organization_id=__TENANT__);
"""
    for table in TABLES
)

READS = {
    "orders_labaccount": (
        "clinic_app.has_permission('demographics.read',clinic_id,NULL)"
    ),
    "orders_order": """clinic_app.has_permission('order.route',clinic_id,NULL)
      OR clinic_app.has_permission('result.read',clinic_id,enrollment_id)
      OR clinic_app.has_permission('order.place',clinic_id,enrollment_id)""",
    "orders_orderitem": """EXISTS(SELECT 1 FROM clinic_app.orders_order o
       WHERE o.id=order_id)""",
    "orders_orderevent": """EXISTS(SELECT 1 FROM clinic_app.orders_order o
       WHERE o.id=order_id)""",
    "orders_result": """EXISTS(SELECT 1 FROM clinic_app.orders_resultversion v
       WHERE v.result_id=orders_result.id
       AND clinic_app.orders_can_read_version(v.id))""",
    "orders_resultversion": "clinic_app.orders_can_read_version(id)",
    **dict.fromkeys(TABLES[6:], "clinic_app.orders_can_read_version(version_id)"),
    "orders_resultobservation": """EXISTS(
      SELECT 1 FROM clinic_app.orders_resultmatch m
      JOIN clinic_app.orders_order o ON o.id=m.order_id
      WHERE m.version_id=orders_resultobservation.version_id
      AND clinic_app.has_permission('result.read',o.clinic_id,o.enrollment_id))""",
}
POLICIES = "".join(
    f"""
GRANT SELECT ON clinic_app.{table} TO clinic_app;
CREATE POLICY orders_read ON clinic_app.{table} FOR SELECT TO clinic_app
 USING (organization_id=__TENANT__ AND ({predicate}));
"""
    for table, predicate in READS.items()
)
INSERTS = {
    "orders_order": "clinic_app.has_permission('order.place',clinic_id,enrollment_id)",
    "orders_orderitem": """EXISTS(SELECT 1 FROM clinic_app.orders_order o
     WHERE o.id=order_id)""",
    "orders_orderevent": """EXISTS(SELECT 1 FROM clinic_app.orders_order o
     WHERE o.id=order_id)""",
    "orders_result": """clinic_app.orders_lab_account(clinic_id) IS NOT NULL
     OR clinic_app.has_permission('demographics.read',clinic_id,NULL)""",
    "orders_resultversion": (
        "clinic_app.orders_ingest_allowed(clinic_id,proposed_order_id)"
    ),
    "orders_resultobservation": """clinic_app.orders_lab_account(clinic_id) IS NOT NULL
     OR EXISTS(SELECT 1 FROM clinic_app.orders_resultversion v
     WHERE v.id=version_id
     AND clinic_app.orders_ingest_allowed(clinic_id,v.proposed_order_id))""",
    "orders_resultrejection": "clinic_app.has_permission('order.route',clinic_id,NULL)",
    "orders_resultacknowledgment": "clinic_app.orders_can_read_version(version_id)",
    "orders_resultrelease": "clinic_app.orders_can_read_version(version_id)",
}
POLICIES += "".join(
    f"""
GRANT INSERT ON clinic_app.{table} TO clinic_app;
CREATE POLICY orders_insert ON clinic_app.{table} FOR INSERT TO clinic_app
 WITH CHECK (organization_id=__TENANT__ AND ({predicate}));
"""
    for table, predicate in INSERTS.items()
)
