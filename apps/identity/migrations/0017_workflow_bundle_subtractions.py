"""Permit subtraction from the unissued workflow successor without changing v1."""

from typing import ClassVar

from django.db import migrations

SQL = """
DO $body$
DECLARE legacy_check text;
BEGIN
 SELECT pg_get_constraintdef(oid) INTO STRICT legacy_check
 FROM pg_constraint
 WHERE conrelid='clinic_app.identity_rolegrant'::regclass
 AND conname='identity_rolegrant_bundle';
 IF left(legacy_check,7)<>'CHECK (' THEN
  RAISE EXCEPTION 'unexpected permission bundle constraint';
 END IF;
 ALTER TABLE clinic_app.identity_rolegrant
 DROP CONSTRAINT identity_rolegrant_bundle;
 ALTER TABLE clinic_app.identity_rolegrant
 ADD CONSTRAINT identity_rolegrant_bundle CHECK (
 CASE role
 WHEN 'allied_professional' THEN permission=ANY(ARRAY['appointment.read',
 'break_glass.request_scoped', 'demographics.read', 'observation.write',
 'order.observe', 'order.task']::text[])
 WHEN 'clinic_admin' THEN permission=ANY(ARRAY['appointment.book', 'appointment.move',
 'appointment.read', 'automation.admin', 'charge.read', 'configuration.clinic',
 'demographics.read', 'payout.request', 'refund.request', 'staff.clinic', 'tiss.read',
 'writeoff.request']::text[])
 WHEN 'clinic_manager' THEN permission=ANY(ARRAY['appointment.book',
 'appointment.move', 'appointment.read', 'automation.admin', 'charge.read',
 'configuration.clinic', 'demographics.read', 'payout.request', 'refund.request',
 'staff.clinic', 'tiss.read', 'writeoff.request']::text[])
 WHEN 'finance' THEN permission=ANY(ARRAY['appointment.read', 'automation.finance',
 'charge.collect', 'charge.create', 'charge.read', 'configuration.clinic',
 'demographics.billing_read', 'payout.approve', 'refund.approve', 'settlement.post',
 'tiss.manage', 'tiss.read', 'writeoff.approve']::text[])
 WHEN 'nurse' THEN permission=ANY(ARRAY['appointment.read',
 'break_glass.request_scoped', 'demographics.read', 'observation.write',
 'order.observe', 'order.task']::text[])
 WHEN 'org_admin' THEN permission=ANY(ARRAY['appointment.read',
 'automation.organization', 'charge.read', 'configuration.organization',
 'finance.policy', 'staff.organization', 'tiss.read']::text[])
 WHEN 'owner' THEN permission=ANY(ARRAY['appointment.read', 'automation.organization',
 'charge.read', 'configuration.organization', 'finance.policy', 'staff.organization',
 'tiss.read']::text[])
 WHEN 'physician' THEN permission=ANY(ARRAY['appointment.book_own',
 'appointment.move_own', 'appointment.read_own', 'automation.propose_clinical',
 'break_glass.request', 'charge.read', 'clinical.amend', 'clinical.finalize',
 'clinical.read', 'clinical.write', 'configuration.propose', 'demographics.read',
 'demographics.write', 'order.place', 'prescription.prepare', 'prescription.sign',
 'result.acknowledge', 'result.read', 'result.release', 'tiss.clinical_read']::text[])
 WHEN 'receptionist' THEN permission=ANY(ARRAY['appointment.book', 'appointment.move',
 'appointment.read', 'charge.collect', 'charge.create', 'charge.read',
 'demographics.read', 'demographics.write', 'order.route']::text[])
 WHEN 'scheduler' THEN permission=ANY(ARRAY['appointment.book', 'appointment.move',
 'appointment.read', 'charge.collect', 'charge.create', 'charge.read',
 'demographics.read', 'demographics.write', 'order.route']::text[])
 ELSE false END
 OR (bundle_version=2 AND (
 (role IN ('owner','org_admin','clinic_admin','clinic_manager',
 'physician','nurse','allied_professional','receptionist','scheduler',
 'finance') AND permission IN ('tasks.view','tasks.assign','tasks.complete'))
 OR (role IN ('owner','org_admin','clinic_admin','clinic_manager')
 AND permission='tasks.reassign'))));
END $body$;
"""


class Migration(migrations.Migration):
    """Keep the catalog's released legacy branch exactly as installed."""

    dependencies: ClassVar = [("identity", "0016_task_workflows")]
    operations: ClassVar = [migrations.RunSQL(SQL)]
