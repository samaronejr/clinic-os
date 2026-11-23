"""Frozen longitudinal contract SQL."""

from . import _longitudinal_sql, _note_access_sql, _timeline_sql

CONTRACT = _longitudinal_sql.render(
    _longitudinal_sql.FUNCTIONS
    + _note_access_sql.FUNCTIONS
    + _timeline_sql.FUNCTIONS
    + _longitudinal_sql.POSTURE
    + _longitudinal_sql.POLICIES
    + _note_access_sql.POLICIES
    + _note_access_sql.TRIGGERS
    + _timeline_sql.POLICIES
    + _timeline_sql.TRIGGERS
    + _timeline_sql.BACKFILL
    + """
SET LOCAL ROLE clinic_resolver;
REVOKE EXECUTE ON FUNCTION clinic_app.ehr_longitudinal_guard(),
 clinic_app.ehr_note_guard(), clinic_app.ehr_copy_finalization_guard(),
 clinic_app.ehr_timeline_enqueue(), clinic_app.questionnaire_immutable()
 FROM clinic_owner;
RESET ROLE;
"""
)
