"""Preserve workflow v2 permissions while refusing retained revoked memberships."""

from collections.abc import Sequence
from typing import ClassVar, Final

from django.db import migrations
from django.db.migrations.operations.base import Operation

SQL: Final = """SET LOCAL ROLE clinic_resolver;
CREATE OR REPLACE FUNCTION clinic_app.has_permission(
 perm text, clinic uuid, enrollment uuid)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
DECLARE actor uuid; tenant uuid; checked_at timestamptz := statement_timestamp();
BEGIN
 BEGIN
  actor := NULLIF(current_setting('app.current_user_id',true),'')::uuid;
  tenant := NULLIF(current_setting('app.current_tenant',true),'')::uuid;
 EXCEPTION WHEN invalid_text_representation THEN RETURN false;
 END;
 IF actor IS NULL OR tenant IS NULL OR clinic IS NULL OR perm IS NULL
 OR NOT EXISTS (SELECT 1 FROM clinic_app.identity_user u WHERE u.id=actor AND
 u.is_active)
 OR NOT EXISTS (SELECT 1 FROM clinic_app.identity_clinic c
   WHERE c.id=clinic AND c.organization_id=tenant)
 THEN RETURN false; END IF;
 IF enrollment IS NOT NULL AND NOT EXISTS (
   SELECT 1 FROM clinic_app.intake_patientclinicenrollment e
   WHERE e.id=enrollment AND e.clinic_id=clinic AND e.organization_id=tenant
 ) THEN RETURN false; END IF;
 RETURN EXISTS (
  SELECT 1 FROM clinic_app.identity_userclinicrole r
  WHERE r.revoked_at IS NULL
  AND r.user_id=actor AND r.clinic_id=clinic AND r.organization_id=tenant
  AND (CASE r.role
 WHEN 'allied_professional' THEN perm=ANY(ARRAY['appointment.read',
 'break_glass.request_scoped', 'demographics.read', 'observation.write',
 'order.observe', 'order.task']::text[])
 WHEN 'clinic_admin' THEN perm=ANY(ARRAY['appointment.book', 'appointment.move',
 'appointment.read', 'automation.admin', 'charge.read', 'configuration.clinic',
 'demographics.read', 'payout.request', 'refund.request', 'staff.clinic', 'tiss.read',
 'writeoff.request']::text[])
 WHEN 'clinic_manager' THEN perm=ANY(ARRAY['appointment.book', 'appointment.move',
 'appointment.read', 'automation.admin', 'charge.read', 'configuration.clinic',
 'demographics.read', 'payout.request', 'refund.request', 'staff.clinic', 'tiss.read',
 'writeoff.request']::text[])
 WHEN 'finance' THEN perm=ANY(ARRAY['appointment.read', 'automation.finance',
 'charge.collect', 'charge.create', 'charge.read', 'configuration.clinic',
 'demographics.billing_read', 'payout.approve', 'refund.approve', 'settlement.post',
 'tiss.manage', 'tiss.read', 'writeoff.approve']::text[])
 WHEN 'nurse' THEN perm=ANY(ARRAY['appointment.read', 'break_glass.request_scoped',
 'demographics.read', 'observation.write', 'order.observe', 'order.task']::text[])
 WHEN 'org_admin' THEN perm=ANY(ARRAY['appointment.read', 'automation.organization',
 'charge.read', 'configuration.organization', 'finance.policy', 'staff.organization',
 'tiss.read']::text[])
 WHEN 'owner' THEN perm=ANY(ARRAY['appointment.read', 'automation.organization',
 'charge.read', 'configuration.organization', 'finance.policy', 'staff.organization',
 'tiss.read']::text[])
 WHEN 'physician' THEN perm=ANY(ARRAY['appointment.book_own', 'appointment.move_own',
 'appointment.read_own', 'automation.propose_clinical', 'break_glass.request',
 'charge.read', 'clinical.amend', 'clinical.finalize', 'clinical.read',
 'clinical.write', 'configuration.propose', 'demographics.read', 'demographics.write',
 'order.place', 'prescription.prepare', 'prescription.sign', 'result.acknowledge',
 'result.read', 'result.release', 'tiss.clinical_read']::text[])
 WHEN 'receptionist' THEN perm=ANY(ARRAY['appointment.book', 'appointment.move',
 'appointment.read', 'charge.collect', 'charge.create', 'charge.read',
 'demographics.read', 'demographics.write', 'order.route']::text[])
 WHEN 'scheduler' THEN perm=ANY(ARRAY['appointment.book', 'appointment.move',
 'appointment.read', 'charge.collect', 'charge.create', 'charge.read',
 'demographics.read', 'demographics.write', 'order.route']::text[])
 ELSE false END OR (r.workflow_bundle_version=2 AND CASE r.role
 WHEN 'owner' THEN perm=ANY(ARRAY[
  'tasks.view','tasks.assign','tasks.complete','tasks.reassign']::text[])
 WHEN 'org_admin' THEN perm=ANY(ARRAY[
  'tasks.view','tasks.assign','tasks.complete','tasks.reassign']::text[])
 WHEN 'clinic_admin' THEN perm=ANY(ARRAY[
  'tasks.view','tasks.assign','tasks.complete','tasks.reassign']::text[])
 WHEN 'clinic_manager' THEN perm=ANY(ARRAY[
  'tasks.view','tasks.assign','tasks.complete','tasks.reassign']::text[])
 WHEN 'physician' THEN perm=ANY(ARRAY[
  'tasks.view','tasks.assign','tasks.complete']::text[])
 WHEN 'nurse' THEN perm=ANY(ARRAY[
  'tasks.view','tasks.assign','tasks.complete']::text[])
 WHEN 'allied_professional' THEN perm=ANY(ARRAY[
  'tasks.view','tasks.assign','tasks.complete']::text[])
 WHEN 'receptionist' THEN perm=ANY(ARRAY[
  'tasks.view','tasks.assign','tasks.complete']::text[])
 WHEN 'scheduler' THEN perm=ANY(ARRAY[
  'tasks.view','tasks.assign','tasks.complete']::text[])
 WHEN 'finance' THEN perm=ANY(ARRAY[
  'tasks.view','tasks.assign','tasks.complete']::text[])
 ELSE false END))
  AND NOT EXISTS (SELECT 1 FROM clinic_app.identity_rolegrant g
    WHERE g.organization_id=tenant AND g.clinic_id=clinic AND g.role=r.role
      AND g.bundle_version IN (1,r.workflow_bundle_version)
      AND g.permission=perm AND g.effect='remove'
      AND g.valid_from<=checked_at AND (g.valid_to IS NULL OR checked_at<g.valid_to))
  AND (NOT perm=ANY(ARRAY['break_glass.request', 'break_glass.request_scoped',
 'clinical.amend', 'clinical.finalize', 'clinical.read', 'clinical.write',
 'observation.write', 'order.observe', 'order.place', 'order.task',
 'prescription.prepare', 'prescription.sign', 'result.acknowledge', 'result.read',
 'result.release', 'tiss.clinical_read']::text[]) OR EXISTS (
    SELECT 1 FROM clinic_app.identity_professionalregistration p
    JOIN clinic_app.identity_clinic c ON c.id=p.clinic_id
    WHERE p.organization_id=tenant AND p.clinic_id=clinic AND p.user_id=actor
      AND p.role=r.role AND p.jurisdiction=c.crm_uf AND p.status='regular'
      AND p.synthetic AND p.revoked_at IS NULL
      AND p.valid_from<=checked_at AND checked_at<p.valid_to
      AND (p.physician_profile_id IS NULL OR EXISTS (
        SELECT 1 FROM clinic_app.identity_physicianprofile legacy
        WHERE legacy.id=p.physician_profile_id AND legacy.organization_id=tenant
          AND legacy.user_id=actor AND legacy.jurisdiction=p.jurisdiction
          AND legacy.synthetic AND legacy.status='regular'
          AND legacy.last_checked_at<=checked_at AND checked_at<legacy.recheck_at
          AND (legacy.expires_at IS NULL OR checked_at<legacy.expires_at)))
  ))
  AND (NOT perm=ANY(ARRAY['clinical.amend', 'clinical.finalize', 'clinical.read',
 'clinical.write', 'observation.write', 'order.observe', 'order.place', 'order.task',
 'prescription.prepare', 'prescription.sign', 'result.acknowledge', 'result.read',
 'result.release', 'tiss.clinical_read']::text[]) OR (enrollment IS NOT NULL AND (
    EXISTS (SELECT 1 FROM clinic_app.identity_careteammembership team
      WHERE team.organization_id=tenant AND team.clinic_id=clinic
        AND team.patient_enrollment_id=enrollment AND team.user_id=actor
        AND team.role=r.role AND team.revoked_at IS NULL
        AND team.valid_from<=checked_at
        AND (team.valid_to IS NULL OR checked_at<team.valid_to))
    OR (r.role='physician' AND EXISTS (
      SELECT 1 FROM clinic_app.ehr_encounter encounter
      JOIN clinic_app.intake_patientclinicenrollment e
        ON e.patient_id=encounter.patient_id AND e.clinic_id=encounter.clinic_id
      WHERE e.id=enrollment AND e.organization_id=tenant
        AND encounter.organization_id=tenant AND encounter.clinic_id=clinic
        AND encounter.physician_id=actor AND encounter.state='open'
    ))
  )))
 );
END $f$;
RESET ROLE;
"""


class Migration(migrations.Migration):
    """Keep released v1 and unissued v2 branches with current membership authority."""

    dependencies: ClassVar[Sequence[tuple[str, str]]] = [
        ("identity", "0029_physician_current_membership"),
    ]
    operations: ClassVar[Sequence[Operation]] = [migrations.RunSQL(SQL)]
