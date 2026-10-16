"""Free-slot projection shares the resource era's closures and buffer semantics."""

from apps.scheduling.migrations._patient_booking_sql import SQL as PATIENT_SQL

SQL = """
SET LOCAL ROLE clinic_resolver;
CREATE OR REPLACE FUNCTION clinic_app.patient_booking_slots(requested_day date,
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
   AND (a.template_id IS NULL OR EXISTS (
     SELECT 1 FROM clinic_app.scheduling_availabilitytemplate t
     WHERE t.id=a.template_id AND t.active))
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
     WHERE booked.status IN ('held','scheduled','arrived','in_progress')
       AND ((booked.practitioner_id = a.practitioner_id
        AND clinic_app.scheduling_effective_interval(booked.start_at,booked.end_at,
            booked.buffer_before,booked.buffer_after)
            && tstzrange(slot.start_at,slot.start_at+interval '30 minutes','[)'))
        OR (booked.patient_id = s.patient_id
         AND booked.start_at<slot.start_at+interval '30 minutes'
         AND booked.end_at>slot.start_at))
       AND (ignored_appointment IS NULL OR booked.id <> ignored_appointment))
   AND NOT EXISTS (SELECT 1 FROM clinic_app.scheduling_holiday h
     WHERE h.clinic_id=s.clinic_id AND h.active
       AND h.start_at<slot.start_at+interval '30 minutes'
       AND h.end_at>slot.start_at)
   AND NOT EXISTS (SELECT 1 FROM clinic_app.scheduling_absence absence
     WHERE absence.clinic_id=s.clinic_id AND absence.active
       AND absence.practitioner_id=a.practitioner_id
       AND absence.start_at<slot.start_at+interval '30 minutes'
       AND absence.end_at>slot.start_at)
 ORDER BY slot.start_at, a.practitioner_id LIMIT 200
$f$;
RESET ROLE;
"""

# The old resolver is restored verbatim, not approximated by a new reverse body.
REVERSE_SQL = (
    "SET LOCAL ROLE clinic_resolver;\n"
    + PATIENT_SQL[
        PATIENT_SQL.index(
            "CREATE FUNCTION clinic_app.patient_booking_slots("
        ) : PATIENT_SQL.index("CREATE FUNCTION clinic_app.patient_booking_guard()")
    ].replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)
    + "RESET ROLE;"
)
