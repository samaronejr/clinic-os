"""Link every referral document that predates care plans, once (todo 52, B7).

Each existing ``PrescriptionDraft`` of kind referral without a coordination row
gets one ``documented`` referral with an unspecified target: nothing about its
destination or progress is inferred, and its creator is the document's issuer.
Runtime code never creates plan-less referrals, so this runs as trusted operator
code: the care guard is disabled for this one statement, inside the migration
transaction, and the resolver's insert grant is temporary. Its foreign keys are
checked when the statement ends: a deferred check still queued would forbid
re-enabling the guard in the same transaction. Re-running links only documents
still unlinked. The reverse keeps the rows; rollback = restore.
"""

from typing import ClassVar, Final

from django.db import migrations

BACKFILL: Final = """
ALTER TABLE clinic_app.workflows_referral DISABLE TRIGGER care_guard;
GRANT INSERT ON clinic_app.workflows_referral TO clinic_resolver;
SET CONSTRAINTS ALL IMMEDIATE;
SET LOCAL ROLE clinic_resolver;
INSERT INTO clinic_app.workflows_referral (id,organization_id,clinic_id,plan_id,
 action_id,target_kind,target_label,status,document_id,created_by_id,revision,
 last_command_key,last_command_digest,created_at,updated_at)
SELECT gen_random_uuid(),d.organization_id,d.clinic_id,NULL,NULL,'unspecified',NULL,
 'documented',d.id,d.issuer_id,1,NULL,'',statement_timestamp(),statement_timestamp()
FROM clinic_app.prescription_prescriptiondraft d
WHERE d.kind='referral' AND NOT EXISTS (SELECT 1 FROM clinic_app.workflows_referral r
 WHERE r.document_id=d.id);
RESET ROLE;
REVOKE INSERT ON clinic_app.workflows_referral FROM clinic_resolver;
ALTER TABLE clinic_app.workflows_referral ENABLE TRIGGER care_guard;
"""


class Migration(migrations.Migration):
    """Additive data step; the reverse leaves the coordination rows in place."""

    dependencies: ClassVar = [("workflows", "0006_care_plans")]
    operations: ClassVar = [migrations.RunSQL(BACKFILL, migrations.RunSQL.noop)]
