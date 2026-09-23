"""Frozen atomic reminder scheduling for both staff and patient bookings."""

SQL = """
ALTER TABLE clinic_app.comms_appointmentreminder ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.comms_appointmentreminder FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON clinic_app.comms_appointmentreminder
 USING (organization_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid)
 WITH CHECK (organization_id =
   NULLIF(current_setting('app.current_tenant', true), '')::uuid);
REVOKE ALL ON clinic_app.comms_appointmentreminder FROM PUBLIC, clinic_app;
GRANT SELECT ON clinic_app.comms_appointmentreminder TO clinic_app;
GRANT SELECT, INSERT ON clinic_app.comms_appointmentreminder TO clinic_resolver;
GRANT INSERT ON clinic_app.comms_integrationoperation TO clinic_resolver;
GRANT UPDATE (status, last_error, updated_at)
 ON clinic_app.comms_integrationoperation TO clinic_resolver;
GRANT SELECT ON clinic_app.intake_patientchannelpreference,
 clinic_app.intake_patientcontact TO clinic_resolver;

SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.comms_schedule_reminders_v1()
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
REVOKE ALL ON FUNCTION clinic_app.comms_schedule_reminders_v1() FROM PUBLIC, clinic_app;

-- Worker enumeration discloses only opaque due job IDs, never destinations.
CREATE FUNCTION clinic_app.comms_due_reminders_v1()
RETURNS SETOF uuid LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
 SELECT id FROM clinic_app.comms_integrationoperation
 WHERE subject_type='scheduling.appointment_reminder'
   AND status IN ('pending', 'in_progress')
   AND not_before <= statement_timestamp()
   AND ((status='pending' AND last_error='')
        OR updated_at <= statement_timestamp()-interval '1 minute')
 ORDER BY not_before, id LIMIT 100
$f$;
REVOKE ALL ON FUNCTION clinic_app.comms_due_reminders_v1() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.comms_due_reminders_v1() TO clinic_app;
GRANT EXECUTE ON FUNCTION clinic_app.comms_schedule_reminders_v1() TO clinic_owner;
RESET ROLE;
CREATE TRIGGER comms_schedule_reminders
 AFTER INSERT OR UPDATE OF start_at, end_at, status ON clinic_app.scheduling_appointment
 FOR EACH ROW EXECUTE FUNCTION clinic_app.comms_schedule_reminders_v1();
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.comms_schedule_reminders_v1() FROM clinic_owner;
RESET ROLE;
"""

REVERSE_SQL = """
DROP TRIGGER comms_schedule_reminders ON clinic_app.scheduling_appointment;
SET LOCAL ROLE clinic_resolver;
DROP FUNCTION clinic_app.comms_schedule_reminders_v1();
DROP FUNCTION clinic_app.comms_due_reminders_v1();
RESET ROLE;
REVOKE INSERT, UPDATE (status, last_error, updated_at)
 ON clinic_app.comms_integrationoperation FROM clinic_resolver;
REVOKE SELECT ON clinic_app.intake_patientchannelpreference,
 clinic_app.intake_patientcontact FROM clinic_resolver;
"""
