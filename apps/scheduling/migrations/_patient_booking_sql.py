"""Narrow patient scheduling authority, availability projection and receipts."""

SQL = """
GRANT SELECT ON clinic_app.scheduling_availabilityblock TO clinic_resolver;
GRANT UPDATE (id) ON clinic_app.scheduling_availabilityblock TO clinic_resolver;
GRANT INSERT ON clinic_app.scheduling_patientbookingevent TO clinic_resolver;
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.patient_booking_scope()
RETURNS TABLE(session_id uuid, organization_id uuid, clinic_id uuid,
 patient_id uuid, enrollment_id uuid, clinic_name varchar, timezone varchar)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
 SELECT s.id, s.organization_id, s.clinic_id, s.patient_id, s.enrollment_id,
        c.name, c.timezone
 FROM clinic_app.intake_patientsession s
 JOIN clinic_app.intake_patientaccessgrant g ON g.id = s.grant_id
 JOIN clinic_app.intake_patientclinicenrollment e ON e.id = s.enrollment_id
   AND e.organization_id = s.organization_id AND e.clinic_id = s.clinic_id
   AND e.patient_id = s.patient_id
 JOIN clinic_app.identity_clinic c ON c.id = s.clinic_id
   AND c.organization_id = s.organization_id
 WHERE s.id = NULLIF(current_setting('app.current_patient_session', true), '')::uuid
   AND s.revoked_at IS NULL AND g.revoked_at IS NULL
   AND s.expires_at > statement_timestamp()
   AND s.idle_expires_at > statement_timestamp()
   AND 'booking' = ANY(s.operations) AND 'booking' = ANY(g.operations)
$f$;
CREATE FUNCTION clinic_app.patient_booking_practitioner(requested_practitioner uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
 SELECT EXISTS (
 SELECT 1 FROM clinic_app.patient_booking_scope() s
 JOIN clinic_app.identity_userclinicrole r ON r.clinic_id = s.clinic_id
   AND r.organization_id = s.organization_id
 JOIN clinic_app.identity_user u ON u.id = r.user_id AND u.is_active
 WHERE r.user_id = requested_practitioner AND r.role = 'physician')
$f$;
CREATE FUNCTION clinic_app.patient_booking_lock_availability(
 practitioners uuid[], starts timestamptz[], ends timestamptz[])
RETURNS SETOF uuid LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
 SELECT a.id FROM clinic_app.scheduling_availabilityblock a
 JOIN clinic_app.patient_booking_scope() s ON s.clinic_id = a.clinic_id
   AND s.organization_id = a.organization_id
 WHERE a.practitioner_id = ANY(practitioners) AND a.retired_at IS NULL
   AND EXISTS (SELECT 1 FROM unnest(starts, ends) r(start_at, end_at)
               WHERE a.start_at <= r.start_at AND a.end_at >= r.end_at)
 ORDER BY a.id FOR UPDATE OF a
$f$;
CREATE FUNCTION clinic_app.patient_booking_slots(requested_day date,
 ignored_appointment uuid DEFAULT NULL)
RETURNS TABLE(practitioner_id uuid, display_label text, start_at timestamptz,
 end_at timestamptz)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
 SELECT a.practitioner_id, u.username::text, slot.start_at,
        slot.start_at + interval '30 minutes'
 FROM clinic_app.patient_booking_scope() s
 JOIN clinic_app.scheduling_availabilityblock a ON a.clinic_id = s.clinic_id
   AND a.organization_id = s.organization_id AND a.retired_at IS NULL
 JOIN clinic_app.identity_user u ON u.id = a.practitioner_id AND u.is_active
 CROSS JOIN LATERAL generate_series(
   greatest(a.start_at, requested_day::timestamp AT TIME ZONE s.timezone),
   least(a.end_at, (requested_day + 1)::timestamp AT TIME ZONE s.timezone)
     - interval '30 minutes', interval '30 minutes') slot(start_at)
 WHERE clinic_app.patient_booking_practitioner(a.practitioner_id)
   AND slot.start_at > statement_timestamp()
   AND (slot.start_at AT TIME ZONE s.timezone)::date = requested_day
   AND ((slot.start_at + interval '30 minutes') AT TIME ZONE s.timezone)::date
       = requested_day
   AND (ignored_appointment IS NULL OR EXISTS (
     SELECT 1 FROM clinic_app.scheduling_appointment own
     WHERE own.id = ignored_appointment AND own.patient_id = s.patient_id
       AND own.clinic_id = s.clinic_id AND own.organization_id = s.organization_id
       AND own.practitioner_id = a.practitioner_id AND own.status = 'scheduled'))
   AND NOT EXISTS (
     SELECT 1 FROM clinic_app.scheduling_appointment booked
     WHERE booked.status = 'scheduled'
       AND (booked.practitioner_id = a.practitioner_id
            OR booked.patient_id = s.patient_id)
       AND booked.start_at < slot.start_at + interval '30 minutes'
       AND booked.end_at > slot.start_at
       AND (ignored_appointment IS NULL OR booked.id <> ignored_appointment))
 ORDER BY slot.start_at, a.practitioner_id LIMIT 200
$f$;
CREATE FUNCTION clinic_app.patient_booking_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
BEGIN
 IF NULLIF(current_setting('app.current_patient_session', true), '') IS NULL THEN
   RETURN NEW;
 END IF;
 IF NOT EXISTS (SELECT 1 FROM clinic_app.patient_booking_scope() s
   WHERE s.organization_id = NEW.organization_id AND s.clinic_id = NEW.clinic_id
     AND s.patient_id = NEW.patient_id) THEN
   RAISE EXCEPTION 'patient booking denied' USING ERRCODE = '42501';
 END IF;
 IF (TG_OP = 'INSERT' AND NEW.status <> 'scheduled')
    OR (NEW.cancellation_reason IS NOT NULL
        AND NEW.cancellation_reason <> 'patient_request')
    OR (TG_OP = 'UPDATE'
        AND NEW.practitioner_id IS DISTINCT FROM OLD.practitioner_id) THEN
   RAISE EXCEPTION 'patient transition denied' USING ERRCODE = '42501';
 END IF;
 RETURN NEW;
END
$f$;
CREATE FUNCTION clinic_app.patient_booking_receipt()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
BEGIN
 INSERT INTO clinic_app.scheduling_patientbookingevent
 (id, organization_id, clinic_id, appointment_id, patient_session_id, action,
  start_at, end_at, created_at)
 SELECT gen_random_uuid(), NEW.organization_id, NEW.clinic_id, NEW.id, s.session_id,
   CASE WHEN TG_OP = 'INSERT' THEN 'created'
        WHEN NEW.status = 'cancelled' THEN 'cancelled' ELSE 'rescheduled' END,
   NEW.start_at, NEW.end_at, statement_timestamp()
 FROM clinic_app.patient_booking_scope() s;
 RETURN NEW;
END
$f$;
CREATE FUNCTION clinic_app.patient_booking_event_immutable()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
BEGIN
 RAISE EXCEPTION 'booking receipts are immutable' USING ERRCODE = '23514';
END
$f$;
REVOKE ALL ON FUNCTION clinic_app.patient_booking_scope(),
 clinic_app.patient_booking_practitioner(uuid),
 clinic_app.patient_booking_lock_availability(uuid[], timestamptz[], timestamptz[]),
 clinic_app.patient_booking_slots(date, uuid), clinic_app.patient_booking_guard(),
 clinic_app.patient_booking_receipt(), clinic_app.patient_booking_event_immutable()
 FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.patient_booking_scope(),
 clinic_app.patient_booking_practitioner(uuid),
 clinic_app.patient_booking_lock_availability(uuid[], timestamptz[], timestamptz[]),
 clinic_app.patient_booking_slots(date, uuid) TO clinic_app;
GRANT EXECUTE ON FUNCTION clinic_app.patient_booking_guard(),
 clinic_app.patient_booking_receipt(), clinic_app.patient_booking_event_immutable()
 TO clinic_owner;
RESET ROLE;
CREATE TRIGGER patient_booking_guard BEFORE INSERT OR UPDATE
 ON clinic_app.scheduling_appointment FOR EACH ROW
 EXECUTE FUNCTION clinic_app.patient_booking_guard();
CREATE TRIGGER patient_booking_receipt AFTER INSERT OR UPDATE
 ON clinic_app.scheduling_appointment FOR EACH ROW
 EXECUTE FUNCTION clinic_app.patient_booking_receipt();
CREATE TRIGGER patient_booking_event_immutable BEFORE UPDATE OR DELETE
 ON clinic_app.scheduling_patientbookingevent FOR EACH ROW
 EXECUTE FUNCTION clinic_app.patient_booking_event_immutable();
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.patient_booking_guard(),
 clinic_app.patient_booking_receipt(), clinic_app.patient_booking_event_immutable()
 FROM clinic_owner;
RESET ROLE;
CREATE POLICY patient_booking_read ON clinic_app.scheduling_availabilityblock
 FOR SELECT TO clinic_app USING (EXISTS (
 SELECT 1 FROM clinic_app.patient_booking_scope() s
 WHERE s.organization_id = scheduling_availabilityblock.organization_id
   AND s.clinic_id = scheduling_availabilityblock.clinic_id));
CREATE POLICY patient_booking_read ON clinic_app.scheduling_appointment
 FOR SELECT TO clinic_app USING (EXISTS (
 SELECT 1 FROM clinic_app.patient_booking_scope() s
 WHERE s.organization_id = scheduling_appointment.organization_id
   AND s.clinic_id = scheduling_appointment.clinic_id
   AND s.patient_id = scheduling_appointment.patient_id));
CREATE POLICY patient_booking_insert ON clinic_app.scheduling_appointment
 FOR INSERT TO clinic_app WITH CHECK (EXISTS (
 SELECT 1 FROM clinic_app.patient_booking_scope() s
 WHERE s.organization_id = scheduling_appointment.organization_id
   AND s.clinic_id = scheduling_appointment.clinic_id
   AND s.patient_id = scheduling_appointment.patient_id));
CREATE POLICY patient_booking_update ON clinic_app.scheduling_appointment
 FOR UPDATE TO clinic_app USING (EXISTS (
 SELECT 1 FROM clinic_app.patient_booking_scope() s
 WHERE s.organization_id = scheduling_appointment.organization_id
   AND s.clinic_id = scheduling_appointment.clinic_id
   AND s.patient_id = scheduling_appointment.patient_id))
 WITH CHECK (EXISTS (
 SELECT 1 FROM clinic_app.patient_booking_scope() s
 WHERE s.organization_id = scheduling_appointment.organization_id
   AND s.clinic_id = scheduling_appointment.clinic_id
   AND s.patient_id = scheduling_appointment.patient_id));
ALTER TABLE clinic_app.scheduling_patientbookingevent ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.scheduling_patientbookingevent FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON clinic_app.scheduling_patientbookingevent
 FOR SELECT USING (organization_id =
 NULLIF(current_setting('app.current_tenant', true), '')::uuid);
REVOKE ALL ON clinic_app.scheduling_patientbookingevent FROM PUBLIC, clinic_app;
GRANT SELECT ON clinic_app.scheduling_patientbookingevent TO clinic_app;
"""

REVERSE_SQL = """
DROP POLICY patient_booking_read ON clinic_app.scheduling_availabilityblock;
DROP POLICY patient_booking_read ON clinic_app.scheduling_appointment;
DROP POLICY patient_booking_insert ON clinic_app.scheduling_appointment;
DROP POLICY patient_booking_update ON clinic_app.scheduling_appointment;
DROP TRIGGER patient_booking_guard ON clinic_app.scheduling_appointment;
DROP TRIGGER patient_booking_receipt ON clinic_app.scheduling_appointment;
DROP TRIGGER patient_booking_event_immutable
 ON clinic_app.scheduling_patientbookingevent;
DROP POLICY tenant_isolation ON clinic_app.scheduling_patientbookingevent;
SET LOCAL ROLE clinic_resolver;
DROP FUNCTION clinic_app.patient_booking_event_immutable();
DROP FUNCTION clinic_app.patient_booking_receipt();
DROP FUNCTION clinic_app.patient_booking_guard();
DROP FUNCTION clinic_app.patient_booking_slots(date, uuid);
DROP FUNCTION clinic_app.patient_booking_lock_availability(
 uuid[], timestamptz[], timestamptz[]);
DROP FUNCTION clinic_app.patient_booking_practitioner(uuid);
DROP FUNCTION clinic_app.patient_booking_scope();
RESET ROLE;
REVOKE SELECT, UPDATE (id)
 ON clinic_app.scheduling_availabilityblock FROM clinic_resolver;
"""
