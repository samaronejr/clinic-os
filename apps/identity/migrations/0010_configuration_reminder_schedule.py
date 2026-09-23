"""Apply the latest bounded schedule only when a reminder snapshot is created."""

from collections.abc import Sequence
from typing import ClassVar

from django.db import migrations
from django.db.migrations.operations.base import Operation

SQL = """
SET LOCAL ROLE clinic_resolver;
CREATE OR REPLACE FUNCTION clinic_app.comms_schedule_reminders_v1()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
DECLARE
 pref record;
 op uuid;
 due timestamptz;
 zone text;
BEGIN
 IF TG_OP = 'UPDATE' AND ROW(NEW.start_at, NEW.end_at, NEW.status)
   IS NOT DISTINCT FROM ROW(OLD.start_at, OLD.end_at, OLD.status) THEN
   RETURN NEW;
 END IF;
 -- Same subject lock as task 13: a committed cancellation cannot be overtaken.
 PERFORM pg_advisory_xact_lock(hashtextextended(
   'clinic-lock-v1:comms-subject:scheduling.appointment_reminder:' || NEW.id, 0));
 UPDATE clinic_app.comms_integrationoperation SET status='cancelled',
   last_error='appointment_changed', updated_at=statement_timestamp()
 WHERE subject_type='scheduling.appointment_reminder' AND subject_id=NEW.id
   AND status IN ('pending', 'in_progress');
 IF NEW.status <> 'scheduled' THEN RETURN NEW; END IF;
 SELECT timezone INTO zone FROM clinic_app.identity_clinic WHERE id=NEW.clinic_id;
 -- Capture the latest clinic schedule only for this new booking/rebooking.
 -- Existing operation due times are immutable configuration receipts.
 due := NEW.start_at - make_interval(hours => coalesce((
   SELECT reminder_hours::integer FROM clinic_app.identity_clinicconfiguration
   WHERE clinic_id=NEW.clinic_id ORDER BY version DESC LIMIT 1),24));
 IF due <= statement_timestamp() THEN RETURN NEW; END IF;
 FOR pref IN
   SELECT p.id, p.channel, p.version, c.destination_version
   FROM clinic_app.intake_patientchannelpreference p
   JOIN clinic_app.intake_patientcontact c ON c.patient_id=p.patient_id
     AND c.organization_id=p.organization_id AND c.channel=p.channel
     AND c.verified_version=c.destination_version
   WHERE p.organization_id=NEW.organization_id AND p.clinic_id=NEW.clinic_id
     AND p.patient_id=NEW.patient_id AND p.purpose='appointment_reminder'
     AND p.opted_in
 LOOP
   op := md5(NEW.id::text || ':' || NEW.updated_at::text || ':' || pref.channel)::uuid;
   INSERT INTO clinic_app.comms_integrationoperation
    (id, organization_id, clinic_id, actor_id, channel, provider, subject_type,
     subject_id, status, attempt_count, max_attempts, last_error,
     idempotency_key, created_at, updated_at, not_before)
   VALUES (op, NEW.organization_id, NEW.clinic_id, NEW.practitioner_id,
     pref.channel, 'reminder-' || pref.channel || '-v1',
     'scheduling.appointment_reminder', NEW.id, 'pending', 0, 3, '',
     op, statement_timestamp(), statement_timestamp(), due)
   ON CONFLICT (organization_id, idempotency_key) DO NOTHING;
   INSERT INTO clinic_app.comms_appointmentreminder
    (operation_id, organization_id, appointment_id, preference_id,
     preference_version, contact_version, start_at, end_at, timezone, template_version)
   VALUES (op, NEW.organization_id, NEW.id, pref.id, pref.version,
     pref.destination_version, NEW.start_at, NEW.end_at, zone, 1)
   ON CONFLICT (operation_id) DO NOTHING;
 END LOOP;
 RETURN NEW;
END $f$;
RESET ROLE;
"""

REVERSE_SQL = """
SET LOCAL ROLE clinic_resolver;
CREATE OR REPLACE FUNCTION clinic_app.comms_schedule_reminders_v1()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
DECLARE
 pref record;
 op uuid;
 due timestamptz;
 zone text;
BEGIN
 IF TG_OP = 'UPDATE' AND ROW(NEW.start_at, NEW.end_at, NEW.status)
   IS NOT DISTINCT FROM ROW(OLD.start_at, OLD.end_at, OLD.status) THEN
   RETURN NEW;
 END IF;
 -- Same subject lock as task 13: a committed cancellation cannot be overtaken.
 PERFORM pg_advisory_xact_lock(hashtextextended(
   'clinic-lock-v1:comms-subject:scheduling.appointment_reminder:' || NEW.id, 0));
 UPDATE clinic_app.comms_integrationoperation SET status='cancelled',
   last_error='appointment_changed', updated_at=statement_timestamp()
 WHERE subject_type='scheduling.appointment_reminder' AND subject_id=NEW.id
   AND status IN ('pending', 'in_progress');
 IF NEW.status <> 'scheduled' THEN RETURN NEW; END IF;
 SELECT timezone INTO zone FROM clinic_app.identity_clinic WHERE id=NEW.clinic_id;
 -- Exactly 24 elapsed hours; render both dates in the captured clinic zone.
 due := NEW.start_at - interval '24 hours';
 IF due <= statement_timestamp() THEN RETURN NEW; END IF;
 FOR pref IN
   SELECT p.id, p.channel, p.version, c.destination_version
   FROM clinic_app.intake_patientchannelpreference p
   JOIN clinic_app.intake_patientcontact c ON c.patient_id=p.patient_id
     AND c.organization_id=p.organization_id AND c.channel=p.channel
     AND c.verified_version=c.destination_version
   WHERE p.organization_id=NEW.organization_id AND p.clinic_id=NEW.clinic_id
     AND p.patient_id=NEW.patient_id AND p.purpose='appointment_reminder'
     AND p.opted_in
 LOOP
   op := md5(NEW.id::text || ':' || NEW.updated_at::text || ':' || pref.channel)::uuid;
   INSERT INTO clinic_app.comms_integrationoperation
    (id, organization_id, clinic_id, actor_id, channel, provider, subject_type,
     subject_id, status, attempt_count, max_attempts, last_error,
     idempotency_key, created_at, updated_at, not_before)
   VALUES (op, NEW.organization_id, NEW.clinic_id, NEW.practitioner_id,
     pref.channel, 'reminder-' || pref.channel || '-v1',
     'scheduling.appointment_reminder', NEW.id, 'pending', 0, 3, '',
     op, statement_timestamp(), statement_timestamp(), due)
   ON CONFLICT (organization_id, idempotency_key) DO NOTHING;
   INSERT INTO clinic_app.comms_appointmentreminder
    (operation_id, organization_id, appointment_id, preference_id,
     preference_version, contact_version, start_at, end_at, timezone, template_version)
   VALUES (op, NEW.organization_id, NEW.id, pref.id, pref.version,
     pref.destination_version, NEW.start_at, NEW.end_at, zone, 1)
   ON CONFLICT (operation_id) DO NOTHING;
 END LOOP;
 RETURN NEW;
END $f$;
RESET ROLE;
"""


class Migration(migrations.Migration):
    """Keep task-19 authorization and delivery semantics; change only due allocation."""

    dependencies: ClassVar[Sequence[tuple[str, str]]] = [
        ("identity", "0009_clinic_configuration_policy"),
        ("comms", "0003_video_channel"),
    ]
    operations: ClassVar[Sequence[Operation]] = [migrations.RunSQL(SQL, REVERSE_SQL)]
