"""Scope professional identities and append-only evidence independently of roles."""

from typing import ClassVar

from django.db import migrations

SQL = """
ALTER TABLE clinic_app.identity_physicianprofile ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.identity_physicianprofile FORCE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.identity_physicianevidence ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.identity_physicianevidence FORCE ROW LEVEL SECURITY;

CREATE POLICY physician_self ON clinic_app.identity_physicianprofile
TO clinic_app USING (
 organization_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid
 AND user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid
 AND EXISTS (SELECT 1 FROM clinic_app.identity_userclinicrole r
   WHERE r.user_id = identity_physicianprofile.user_id
   AND r.organization_id = identity_physicianprofile.organization_id
   AND r.role = 'physician')
);
CREATE POLICY physician_provision ON clinic_app.identity_physicianprofile
TO clinic_owner USING (
 organization_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid
);
CREATE POLICY physician_evidence_assigned ON clinic_app.identity_physicianevidence
TO clinic_app USING (
 EXISTS (SELECT 1 FROM clinic_app.identity_physicianprofile p
 JOIN clinic_app.ehr_encounter e ON e.id = encounter_id
 WHERE p.id = profile_id AND p.organization_id = e.organization_id
 AND p.user_id = e.physician_id AND p.jurisdiction =
   (SELECT c.crm_uf FROM clinic_app.identity_clinic c WHERE c.id = e.clinic_id)
 AND clinic_app.ehr_assigned(e.id))
);
CREATE POLICY physician_evidence_owner ON clinic_app.identity_physicianevidence
FOR SELECT TO clinic_owner USING (
 EXISTS (SELECT 1 FROM clinic_app.identity_physicianprofile p WHERE p.id = profile_id)
);

REVOKE ALL ON clinic_app.identity_physicianprofile,
 clinic_app.identity_physicianevidence FROM PUBLIC, clinic_app;
GRANT SELECT ON clinic_app.identity_physicianprofile TO clinic_app;
GRANT UPDATE (status, expires_at, last_checked_at, recheck_at)
 ON clinic_app.identity_physicianprofile TO clinic_app;
GRANT SELECT, INSERT ON clinic_app.identity_physicianevidence TO clinic_app;
"""

REVERSE_SQL = """
DROP POLICY physician_evidence_owner ON clinic_app.identity_physicianevidence;
DROP POLICY physician_evidence_assigned ON clinic_app.identity_physicianevidence;
DROP POLICY physician_provision ON clinic_app.identity_physicianprofile;
DROP POLICY physician_self ON clinic_app.identity_physicianprofile;
REVOKE ALL ON clinic_app.identity_physicianprofile,
 clinic_app.identity_physicianevidence FROM clinic_app;
ALTER TABLE clinic_app.identity_physicianprofile DISABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.identity_physicianprofile NO FORCE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.identity_physicianevidence DISABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.identity_physicianevidence NO FORCE ROW LEVEL SECURITY;
"""


class Migration(migrations.Migration):
    """Keep identity provisioning owner-only and evidence runtime append-only."""

    dependencies: ClassVar = [("identity", "0006_physician_verification")]
    operations: ClassVar = [migrations.RunSQL(SQL, REVERSE_SQL)]
