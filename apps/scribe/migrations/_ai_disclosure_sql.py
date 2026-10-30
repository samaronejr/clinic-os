"""Require encounter-specific AI disclosure at every capture SQL boundary."""

from apps.teleconsult.migrations._consented_capture_sql import (
    SQL as CONSENTED_CAPTURE_SQL,
)

from ._capture_sql import SQL as CAPTURE_SQL

_guard_start = CAPTURE_SQL.index("CREATE FUNCTION clinic_app.scribe_guard()")
_guard_end = CAPTURE_SQL.index("\n$f$;", _guard_start) + len("\n$f$;")
_previous_guard = CAPTURE_SQL[_guard_start:_guard_end].replace(
    "CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1
)
_current_guard = _previous_guard.replace(
    "NEW.consent_id,NEW.clinic_id,NEW.enrollment_id)",
    "NEW.consent_id,NEW.clinic_id,NEW.enrollment_id,NEW.encounter_id)",
).replace(
    "s.consent_id,s.clinic_id,s.enrollment_id)",
    "s.consent_id,s.clinic_id,s.enrollment_id,s.encounter_id)",
)

_room_start = CONSENTED_CAPTURE_SQL.index(
    "CREATE FUNCTION clinic_app.scribe_room_capture_allowed("
)
_room_end = CONSENTED_CAPTURE_SQL.index("\n$f$;", _room_start) + len("\n$f$;")
_previous_room = CONSENTED_CAPTURE_SQL[_room_start:_room_end].replace(
    "CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1
)
_current_room = _previous_room.replace(
    "s.consent_id,s.clinic_id,s.enrollment_id)",
    "s.consent_id,s.clinic_id,s.enrollment_id,s.encounter_id)",
)

_forward_parts = (
    """
GRANT SELECT (encounter_id,clinic_id,organization_id,patient_id,informed,refused)
 ON clinic_app.consent_aiusedisclosure TO clinic_resolver;
SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.scribe_consent(
 acceptance uuid,clinic uuid,enrollment uuid,requested_encounter uuid)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,clinic_app,pg_temp AS $f$
 SELECT clinic_app.scribe_consent(acceptance,clinic,enrollment)
 AND EXISTS (
  SELECT 1 FROM clinic_app.ehr_encounter e
  JOIN clinic_app.consent_aiusedisclosure d ON d.encounter_id=e.id
  JOIN clinic_app.intake_patientclinicenrollment p ON p.id=enrollment
  WHERE e.id=requested_encounter AND e.state='open'
  AND e.clinic_id=clinic AND p.clinic_id=e.clinic_id
  AND p.organization_id=e.organization_id AND p.patient_id=e.patient_id
  AND d.clinic_id=e.clinic_id AND d.organization_id=e.organization_id
  AND d.patient_id=e.patient_id AND d.informed AND NOT d.refused)
$f$;
REVOKE ALL ON FUNCTION clinic_app.scribe_consent(uuid,uuid,uuid,uuid)
 FROM PUBLIC,clinic_app;
""",
    _current_guard,
    _current_room,
    """
UPDATE clinic_app.teleconsult_teleconsultroom r
 SET recording_enabled=false,transcription_enabled=false
 WHERE (r.recording_enabled OR r.transcription_enabled)
 AND NOT clinic_app.scribe_room_capture_allowed(r.session_id);
RESET ROLE;
""",
)
SQL = "\n".join(_forward_parts)

_reverse_parts = (
    "SET LOCAL ROLE clinic_resolver;",
    _previous_guard,
    _previous_room,
    """
DROP FUNCTION clinic_app.scribe_consent(uuid,uuid,uuid,uuid);
RESET ROLE;
REVOKE SELECT (encounter_id,clinic_id,organization_id,patient_id,informed,refused)
 ON clinic_app.consent_aiusedisclosure FROM clinic_resolver;
""",
)
REVERSE_SQL = "\n".join(_reverse_parts)
