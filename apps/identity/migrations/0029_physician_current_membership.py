"""Preserve professional records without exposing them through revoked roles."""

from collections.abc import Sequence
from typing import ClassVar, Final

from django.db import migrations
from django.db.migrations.operations.base import Operation

SQL: Final = """
ALTER POLICY physician_self ON clinic_app.identity_physicianprofile
USING (
 organization_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid
 AND user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid
 AND EXISTS (SELECT 1 FROM clinic_app.identity_userclinicrole r
   WHERE r.user_id = identity_physicianprofile.user_id
   AND r.organization_id = identity_physicianprofile.organization_id
   AND r.revoked_at IS NULL
   AND r.role = 'physician')
);
ALTER POLICY physician_evidence_assigned ON clinic_app.identity_physicianevidence
USING (
 EXISTS (SELECT 1 FROM clinic_app.identity_physicianprofile p
   JOIN clinic_app.ehr_encounter e ON e.id = encounter_id
   WHERE p.id = profile_id AND p.organization_id = e.organization_id
   AND p.user_id = e.physician_id AND p.jurisdiction =
     (SELECT c.crm_uf FROM clinic_app.identity_clinic c WHERE c.id = e.clinic_id)
   AND clinic_app.ehr_assigned(e.id)
   AND EXISTS (SELECT 1 FROM clinic_app.identity_userclinicrole r
     WHERE r.user_id = p.user_id AND r.organization_id = p.organization_id
     AND r.clinic_id = e.clinic_id AND r.role = 'physician'
     AND r.revoked_at IS NULL))
);
"""


class Migration(migrations.Migration):
    """Require active canonical membership for profile and evidence visibility."""

    dependencies: ClassVar[Sequence[tuple[str, str]]] = [
        ("identity", "0028_savedview_current_membership"),
    ]
    operations: ClassVar[Sequence[Operation]] = [migrations.RunSQL(SQL)]
