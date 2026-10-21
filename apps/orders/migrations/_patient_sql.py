"""Patient/delegate reads revalidate the exact audience and current source."""

FUNCTIONS = """
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.orders_patient_sources(requested_version uuid)
RETURNS TABLE(version_id uuid,number integer,created_at timestamptz,
 storage_key text,sha256 text,size_bytes integer,content_type text)
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT v.id,v.number,v.created_at,v.storage_key::text,v.sha256::text,
 v.size_bytes,v.content_type::text
 FROM clinic_app.current_patient_authority('records') a
 JOIN clinic_app.orders_order o ON o.enrollment_id=a.enrollment_id
 AND o.organization_id=a.organization_id AND o.clinic_id=a.clinic_id
 JOIN clinic_app.orders_resultmatch m ON m.order_id=o.id
 JOIN clinic_app.orders_resultversion v ON v.id=m.version_id
 WHERE (requested_version IS NULL OR v.id=requested_version)
 AND NOT EXISTS(SELECT 1 FROM clinic_app.orders_resultversion newer
 WHERE newer.result_id=v.result_id AND newer.number>v.number)
 AND EXISTS(SELECT 1 FROM clinic_app.orders_resultrelease r
 JOIN clinic_app.orders_resultacknowledgment ack
 ON ack.id=r.acknowledgment_id AND ack.version_id=v.id
 WHERE r.version_id=v.id AND (
 (a.delegate_grant_id IS NULL AND r.audience='patient') OR
 (a.delegate_grant_id IS NOT NULL AND r.audience='delegate'
  AND r.delegate_grant_id=a.delegate_grant_id
  AND clinic_app.orders_delegate_allowed(v.id,r.delegate_grant_id))))
 ORDER BY v.created_at DESC,v.id
$f$;
REVOKE ALL ON FUNCTION clinic_app.orders_patient_sources(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.orders_patient_sources(uuid) TO clinic_app;
RESET ROLE;
"""
