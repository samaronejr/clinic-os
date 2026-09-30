"""Bind new v2 event roles to their current participant authority additively."""

from ._teleconsult_v2_sql import BINDING_GUARD_V2

_EVENT_BRANCH = " ELSIF TG_TABLE_NAME='teleconsult_teleconsultevent' THEN\n"
_AUTHORITY = """   IF NEW.kind IN (
        'reconnected','audio_only','video_restored','removed')
      AND NOT ((NEW.actor_role='physician'
                AND clinic_app.teleconsult_clinician(s.id))
           OR (NEW.actor_role='patient'
               AND clinic_app.teleconsult_patient_match(s.id))) THEN
     RAISE EXCEPTION 'teleconsult participant authority required'
       USING ERRCODE='42501';
   END IF;
"""

BINDING_GUARD = BINDING_GUARD_V2.replace(_EVENT_BRANCH, _EVENT_BRANCH + _AUTHORITY)
SQL = "SET LOCAL ROLE clinic_resolver;\n" + BINDING_GUARD + "\nRESET ROLE;"
REVERSE_SQL = "SET LOCAL ROLE clinic_resolver;\n" + BINDING_GUARD_V2 + "\nRESET ROLE;"
