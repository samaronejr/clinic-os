"""Delegate access in the patient access log (todo 73, threat row DG-R1).

``identity_delegateaccessevent`` holds one row per delegate read. Only the
resolver-owned ``record_delegate_access`` writes it, from the current patient
session's own delegate binding; the runtime role has no privilege on the table.
``enterprise_patient_access_log`` gains those rows for the represented
patient's own session, next to break-glass reads.
"""

from __future__ import annotations

import re
from typing import Final

from apps.identity.enterprise_contract import TABLES as _ENTERPRISE_TABLES
from apps.identity.migrations._enterprise_access_sql import SQL as _ENTERPRISE_ACCESS

_TENANT: Final = (
    "organization_id=NULLIF(current_setting('app.current_tenant',true),'')::uuid"
)
_LOG_COLUMNS: Final = (
    "notice_id uuid,occurred_at timestamptz,clinician_label text,"
    "expires_at timestamptz,reviewed boolean,clinic_timezone text,"
    "kind text,record_type text,grant_id uuid,delegate_kind text"
)
_BREAK_GLASS: Final = """
 SELECT notice.id,notice.created_at,clinician.username::text,g.expires_at,
  g.reviewed_at IS NOT NULL,unit.timezone::text,'emergency'::text,NULL::text,
  NULL::uuid,NULL::text
 FROM clinic_app.current_patient_authority('enrollment_view') scope
 JOIN clinic_app.identity_breakglassgrant g ON g.enrollment_id=scope.enrollment_id
 AND g.organization_id=scope.organization_id AND g.clinic_id=scope.clinic_id
 JOIN clinic_app.identity_governancenotice notice ON notice.break_glass_id=g.id
 AND notice.audience='patient'
 JOIN clinic_app.identity_user clinician ON clinician.id=g.clinician_id
 JOIN clinic_app.identity_clinic unit ON unit.id=scope.clinic_id
 WHERE scope.clinic_id=clinic AND scope.delegate_grant_id IS NULL"""
_DELEGATE: Final = """
 SELECT e.id,e.accessed_at,NULL::text,NULL::timestamptz,true,unit.timezone::text,
  'delegate'::text,e.record_type::text,e.grant_id,d.authority_kind::text
 FROM clinic_app.current_patient_authority('enrollment_view') scope
 JOIN clinic_app.identity_delegateaccessevent e ON e.enrollment_id=scope.enrollment_id
 AND e.organization_id=scope.organization_id AND e.clinic_id=scope.clinic_id
 JOIN clinic_app.identity_patientdelegategrant d ON d.id=e.grant_id
 JOIN clinic_app.identity_clinic unit ON unit.id=scope.clinic_id
 WHERE scope.clinic_id=clinic AND scope.delegate_grant_id IS NULL"""


_LOG_TEMPLATE: Final = """
DROP FUNCTION clinic_app.enterprise_patient_access_log(
 uuid,integer);
CREATE FUNCTION clinic_app.enterprise_patient_access_log(
 clinic uuid, page_number integer)
RETURNS TABLE(__columns__)
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,pg_temp AS $f$
 SELECT * FROM (__body__) entry
 WHERE page_number BETWEEN 0 AND 10000
 ORDER BY 2 DESC,1
 LIMIT 100 OFFSET (page_number*100)
$f$;
REVOKE ALL ON FUNCTION clinic_app.enterprise_patient_access_log(uuid,integer)
 FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.enterprise_patient_access_log(uuid,integer)
 TO clinic_app;
"""


SQL: Final = """
REVOKE ALL ON clinic_app.identity_delegateaccessevent FROM PUBLIC,clinic_app;
ALTER TABLE clinic_app.identity_delegateaccessevent ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.identity_delegateaccessevent FORCE ROW LEVEL SECURITY;
CREATE POLICY delegate_access_owner ON clinic_app.identity_delegateaccessevent
 TO clinic_owner USING (__tenant__) WITH CHECK (__tenant__);
GRANT SELECT,INSERT ON clinic_app.identity_delegateaccessevent TO clinic_resolver;
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.record_delegate_access(requested_type text)
RETURNS void LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,pg_temp AS $f$
 INSERT INTO clinic_app.identity_delegateaccessevent(
  id,organization_id,clinic_id,grant_id,enrollment_id,record_type,accessed_at)
 SELECT gen_random_uuid(),a.organization_id,a.clinic_id,a.delegate_grant_id,
  a.enrollment_id,requested_type,statement_timestamp()
 FROM clinic_app.current_patient_authority('enrollment_view') a
 WHERE a.delegate_grant_id IS NOT NULL
$f$;
REVOKE ALL ON FUNCTION clinic_app.record_delegate_access(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.record_delegate_access(text) TO clinic_app;
__log__
RESET ROLE;
""".replace("__tenant__", _TENANT).replace(
    "__log__",
    _LOG_TEMPLATE.replace("__columns__", _LOG_COLUMNS).replace(
        "__body__", _BREAK_GLASS + "\n UNION ALL" + _DELEGATE
    ),
)

_ORIGINAL_LOG: Final = re.search(
    r"CREATE FUNCTION clinic_app\.enterprise_patient_access_log\(.*?\$f\$;",
    _ENTERPRISE_ACCESS,
    re.DOTALL,
)
if _ORIGINAL_LOG is None:  # pragma: no cover - import-time contract
    _MISSING = "todo 65 access-log definition not found"
    raise RuntimeError(_MISSING)
# Like 0051, the reverse is the unused rollback only: it refuses, before any
# change, while an enterprise or delegate-access row exists.
_POPULATED: Final = "\n OR ".join(
    "EXISTS (SELECT 1 FROM clinic_app.__TABLE__)".replace("__TABLE__", table)
    for table in (*_ENTERPRISE_TABLES, "identity_delegateaccessevent")
)
REVERSE_SQL: Final = """
SET LOCAL ROLE clinic_resolver;
DO $populated$
BEGIN
 IF __POPULATED__ THEN
 RAISE EXCEPTION 'populated enterprise identity rolls back only by restore'
 USING ERRCODE='55000';
 END IF;
END $populated$;
DROP FUNCTION clinic_app.record_delegate_access(text);
DROP FUNCTION clinic_app.enterprise_patient_access_log(uuid,integer);
__original__
REVOKE ALL ON FUNCTION clinic_app.enterprise_patient_access_log(uuid,integer)
 FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.enterprise_patient_access_log(uuid,integer)
 TO clinic_app;
RESET ROLE;
DROP POLICY delegate_access_owner ON clinic_app.identity_delegateaccessevent;
""".replace("__original__", _ORIGINAL_LOG.group(0)).replace("__POPULATED__", _POPULATED)
