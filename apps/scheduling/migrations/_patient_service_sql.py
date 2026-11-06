"""Patients book service types through the shared todo 21 write path (todo 50).

Patient sessions read only their clinic's service types, resources and
availability templates, so ``booking_selection`` and
``validate_resource_window`` run unchanged. A resolver
projects free service slots with one free resource of every required kind, and
the capacity guard admits a patient INSERT for the session's own patient while
still deciding duration, professional roles, resources, availability and
capacity exactly as it does for staff.

Service self-booking needs the clinic's direct-booking policy. Where
``self_booking_requires_approval`` is on, a patient's booking is born
``requested``, and todo 22 closed the service state machine over the occupying
states only, so a requested service row has no edges. The lifecycle guard
therefore keeps refusing a patient service insert under that policy, the
resolver offers no slot and the projection lists no service, so the portal
never shows a choice it cannot honour.
"""

from apps.scheduling.migrations._capacity_insert_sql import _INSERT_CHECK
from apps.scheduling.migrations._lifecycle_sql import (
    _CAPACITY_V2,
    _GUARD_SQL_PART_8,
)

_STAFF_ONLY = " '')::uuid)) THEN\n"
_PATIENT_TOO = (
    " '')::uuid)\n"
    "     OR EXISTS (SELECT 1 FROM clinic_app.patient_booking_scope() scope\n"
    "      WHERE scope.clinic_id=NEW.clinic_id\n"
    "       AND scope.organization_id=NEW.organization_id\n"
    "       AND scope.patient_id=NEW.patient_id)) THEN\n"
)
if _CAPACITY_V2.count(_INSERT_CHECK) != 1 or _INSERT_CHECK.count(_STAFF_ONLY) != 1:
    message = "capacity guard insert authority moved"
    raise RuntimeError(message)
_CAPACITY_V3 = _CAPACITY_V2.replace(
    _INSERT_CHECK, _INSERT_CHECK.replace(_STAFF_ONLY, _PATIENT_TOO, 1), 1
)

_LIFECYCLE_HEADER = "CREATE FUNCTION clinic_app.scheduling_lifecycle_guard()"
_LIFECYCLE = (
    "CREATE OR REPLACE FUNCTION clinic_app.scheduling_lifecycle_guard()"
    + _GUARD_SQL_PART_8.split(_LIFECYCLE_HEADER, 1)[1].split("END $f$;", 1)[0]
    + "END $f$;\n"
)
_SERVICE_REFUSAL = (
    "   -- Patients never create service bookings (no todo 21 book authority).\n"
    "   IF NEW.service_type_id IS NOT NULL THEN\n"
    "    RAISE EXCEPTION 'scheduling access denied' USING ERRCODE='42501';\n"
    "   END IF;\n"
)
_SERVICE_ADMISSION = (
    "   -- Todo 50: patients book service types where the clinic books them\n"
    "   -- directly; the capacity guard decides the service, its professional\n"
    "   -- roles, resources, availability and capacity. Under the approval\n"
    "   -- policy a patient row is born requested, which todo 22 gave service\n"
    "   -- bookings no edges for, so that combination stays refused.\n"
    "   IF NEW.service_type_id IS NOT NULL AND approval THEN\n"
    "    RAISE EXCEPTION 'scheduling access denied' USING ERRCODE='42501';\n"
    "   END IF;\n"
)
if _LIFECYCLE.count(_SERVICE_REFUSAL) != 1:
    message = "lifecycle guard patient service refusal moved"
    raise RuntimeError(message)
_LIFECYCLE_V2 = _LIFECYCLE.replace(_SERVICE_REFUSAL, _SERVICE_ADMISSION, 1)

POLICY_SQL = """
CREATE POLICY patient_booking_read ON clinic_app.scheduling_servicetype
 FOR SELECT TO clinic_app USING (EXISTS (
  SELECT 1 FROM clinic_app.patient_booking_scope() s
  WHERE s.organization_id=scheduling_servicetype.organization_id
   AND s.clinic_id=scheduling_servicetype.clinic_id));
CREATE POLICY patient_booking_read ON clinic_app.scheduling_resource
 FOR SELECT TO clinic_app USING (EXISTS (
  SELECT 1 FROM clinic_app.patient_booking_scope() s
  WHERE s.organization_id=scheduling_resource.organization_id
   AND s.clinic_id=scheduling_resource.clinic_id));
CREATE POLICY patient_booking_read ON clinic_app.scheduling_availabilitytemplate
 FOR SELECT TO clinic_app USING (EXISTS (
  SELECT 1 FROM clinic_app.patient_booking_scope() s
  WHERE s.organization_id=scheduling_availabilitytemplate.organization_id
   AND s.clinic_id=scheduling_availabilitytemplate.clinic_id));
"""

SLOTS_SQL = """
CREATE FUNCTION clinic_app.patient_service_slots(requested_day date,
 requested_service uuid)
RETURNS TABLE(practitioner_id uuid, display_label text, start_at timestamptz,
 end_at timestamptz, resource_ids uuid[])
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp AS $f$
DECLARE scope RECORD; service clinic_app.scheduling_servicetype;
 visit_length interval; pad_before interval; pad_after interval;
 needs_approval boolean;
BEGIN
 SELECT * INTO scope FROM clinic_app.patient_booking_scope();
 IF scope.session_id IS NULL OR requested_day IS NULL THEN
   RAISE EXCEPTION 'patient booking denied' USING ERRCODE='42501';
 END IF;
 SELECT c.self_booking_requires_approval INTO needs_approval
  FROM clinic_app.identity_clinicconfiguration c
  WHERE c.clinic_id=scope.clinic_id AND c.organization_id=scope.organization_id
  ORDER BY c.version DESC LIMIT 1;
 IF coalesce(needs_approval,false) THEN
   RAISE EXCEPTION 'patient booking denied' USING ERRCODE='42501';
 END IF;
 SELECT * INTO service FROM clinic_app.scheduling_servicetype t
 WHERE t.id=requested_service AND t.clinic_id=scope.clinic_id
   AND t.organization_id=scope.organization_id AND t.active;
 IF service.id IS NULL THEN
   RAISE EXCEPTION 'patient booking denied' USING ERRCODE='42501';
 END IF;
 visit_length:=service.duration_min*interval '1 minute';
 pad_before:=service.buffer_before*interval '1 minute';
 pad_after:=service.buffer_after*interval '1 minute';
 RETURN QUERY
 WITH candidate AS (
  SELECT a.practitioner_id AS practitioner, u.username::text AS label,
   slot.at AS slot_start, slot.at+visit_length AS slot_end,
   tstzrange(slot.at-pad_before,slot.at+visit_length+pad_after,'[)') AS span
  FROM generate_series(requested_day::timestamp AT TIME ZONE scope.timezone,
    (requested_day+1)::timestamp AT TIME ZONE scope.timezone-visit_length,
    interval '15 minutes') slot(at)
  JOIN clinic_app.scheduling_availabilityblock a
    ON a.clinic_id=scope.clinic_id AND a.organization_id=scope.organization_id
   AND a.practitioner_id IS NOT NULL AND a.retired_at IS NULL
   AND a.start_at<=slot.at-pad_before
   AND a.end_at>=slot.at+visit_length+pad_after
   AND (a.template_id IS NULL OR EXISTS (
     SELECT 1 FROM clinic_app.scheduling_availabilitytemplate t
     WHERE t.id=a.template_id AND t.active))
  JOIN clinic_app.identity_user u ON u.id=a.practitioner_id AND u.is_active
  WHERE slot.at>statement_timestamp()
   AND clinic_app.patient_booking_practitioner(a.practitioner_id)
   AND EXISTS (SELECT 1 FROM clinic_app.identity_userclinicrole role
     WHERE role.user_id=a.practitioner_id AND role.clinic_id=scope.clinic_id
      AND role.organization_id=scope.organization_id AND role.revoked_at IS NULL
      AND role.role=ANY(service.required_professional_roles))
   AND NOT EXISTS (SELECT 1 FROM clinic_app.scheduling_appointment booked
     WHERE booked.organization_id=scope.organization_id
      AND booked.status IN ('held','scheduled','arrived','in_progress')
      AND ((booked.practitioner_id=a.practitioner_id
        AND clinic_app.scheduling_effective_interval(booked.start_at,
          booked.end_at,booked.buffer_before,booked.buffer_after)
          && tstzrange(slot.at-pad_before,slot.at+visit_length+pad_after,'[)'))
       OR (booked.patient_id=scope.patient_id
        AND booked.start_at<slot.at+visit_length AND booked.end_at>slot.at)))
   AND NOT EXISTS (SELECT 1 FROM clinic_app.scheduling_holiday h
     WHERE h.clinic_id=scope.clinic_id AND h.organization_id=scope.organization_id
      AND h.active AND tstzrange(h.start_at,h.end_at,'[)')
       && tstzrange(slot.at-pad_before,slot.at+visit_length+pad_after,'[)'))
   AND NOT EXISTS (SELECT 1 FROM clinic_app.scheduling_absence absence
     WHERE absence.clinic_id=scope.clinic_id
      AND absence.organization_id=scope.organization_id AND absence.active
      AND absence.practitioner_id=a.practitioner_id
      AND tstzrange(absence.start_at,absence.end_at,'[)')
       && tstzrange(slot.at-pad_before,slot.at+visit_length+pad_after,'[)'))
 )
 SELECT c.practitioner, c.label, c.slot_start, c.slot_end,
   coalesce(pick.ids,'{}'::uuid[])
 FROM candidate c
 CROSS JOIN LATERAL (
  SELECT array_agg(chosen.id ORDER BY chosen.id) AS ids, count(chosen.id) AS found
  FROM (SELECT DISTINCT need.kind
    FROM unnest(service.required_resource_kinds) need(kind)) wanted
  CROSS JOIN LATERAL (
   SELECT r.id FROM clinic_app.scheduling_resource r
   WHERE r.clinic_id=scope.clinic_id AND r.organization_id=scope.organization_id
    AND r.active AND r.kind=wanted.kind
    AND EXISTS (SELECT 1 FROM clinic_app.scheduling_availabilityblock b
     WHERE b.resource_id=r.id AND b.clinic_id=scope.clinic_id
      AND b.organization_id=scope.organization_id AND b.retired_at IS NULL
      AND b.start_at<=lower(c.span) AND b.end_at>=upper(c.span)
      AND (b.template_id IS NULL OR EXISTS (
        SELECT 1 FROM clinic_app.scheduling_availabilitytemplate t
        WHERE t.id=b.template_id AND t.active)))
    AND NOT EXISTS (SELECT 1 FROM clinic_app.scheduling_absence absence
     WHERE absence.clinic_id=scope.clinic_id
      AND absence.organization_id=scope.organization_id AND absence.active
      AND absence.resource_id=r.id
      AND tstzrange(absence.start_at,absence.end_at,'[)') && c.span)
    AND (SELECT count(DISTINCT used.unit)
     FROM clinic_app.scheduling_appointmentresource used
     WHERE used.resource_id=r.id AND used.occupied
      AND tstzrange(used.start_at,used.end_at,'[)') && c.span) < r.capacity
   ORDER BY r.id LIMIT 1) chosen
 ) pick
 WHERE pick.found=(SELECT count(DISTINCT need.kind)
   FROM unnest(service.required_resource_kinds) need(kind))
 ORDER BY c.slot_start, c.practitioner LIMIT 200;
END
$f$;
REVOKE ALL ON FUNCTION clinic_app.patient_service_slots(date,uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.patient_service_slots(date,uuid)
 TO clinic_app;
"""

SQL = (
    POLICY_SQL
    + "SET LOCAL ROLE clinic_resolver;\n"
    + SLOTS_SQL
    + _CAPACITY_V3
    + _LIFECYCLE_V2
    + "RESET ROLE;\n"
)
REVERSE_SQL = (
    "SET LOCAL ROLE clinic_resolver;\n"
    "DROP FUNCTION clinic_app.patient_service_slots(date,uuid);\n"
    + _CAPACITY_V2
    + _LIFECYCLE
    + "RESET ROLE;\n"
    + "DROP POLICY patient_booking_read\n"
    " ON clinic_app.scheduling_availabilitytemplate;\n"
    "DROP POLICY patient_booking_read ON clinic_app.scheduling_resource;\n"
    "DROP POLICY patient_booking_read ON clinic_app.scheduling_servicetype;\n"
)
