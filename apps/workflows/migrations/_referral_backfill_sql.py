"""Frozen SQL for the referral backfill migration."""

from typing import Final

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
