"""Split Wave D's shared longitudinal guard into one guard per trigger table.

Each guard keeps exactly its table's branch of the released guard, so every
NEW column it reads belongs to the table that fires it.
"""

from apps.ehr.migrations._longitudinal_sql import FUNCTIONS, render

_HEADER = "CREATE FUNCTION clinic_app.ehr_longitudinal_guard()"
_START = FUNCTIONS.index(_HEADER)
_END = FUNCTIONS.index("END $f$;", _START) + len("END $f$;")
SHARED = render(FUNCTIONS[_START:_END])
_IF = " IF TG_TABLE_NAME='ehr_medicationstatement' THEN\n"
_ELSE = " ELSE\n"
_FI = " END IF;\n IF TG_OP='INSERT' THEN"
# Only observations refuse re-confirmation; for medications the clause is false.
_CONFIRMED = (
    "\n      OR (TG_TABLE_NAME='ehr_observation'"
    "\n          AND OLD.provenance='clinician_confirmed')"
)
if SHARED.count(_IF) != 1 or SHARED.count(_CONFIRMED) != 1:  # pragma: no cover
    message = "released longitudinal guard anchors moved"
    raise RuntimeError(message)
_BRANCH = SHARED.index(_IF)
_OTHERWISE = SHARED.index(_ELSE, _BRANCH)
_JOIN = SHARED.index(_FI, _OTHERWISE)
_PREFIX = SHARED[:_BRANCH]
_SUFFIX = SHARED[_JOIN + len(" END IF;\n") :]
MEDICATION = (
    _PREFIX.replace(
        _HEADER, "CREATE FUNCTION clinic_app.ehr_medicationstatement_guard()", 1
    )
    + SHARED[_BRANCH + len(_IF) : _OTHERWISE]
    + _SUFFIX.replace(_CONFIRMED, "", 1)
)
OBSERVATION = (
    _PREFIX.replace(_HEADER, "CREATE FUNCTION clinic_app.ehr_observation_guard()", 1)
    + SHARED[_OTHERWISE + len(_ELSE) : _JOIN]
    + _SUFFIX.replace(_CONFIRMED, "\n      OR OLD.provenance='clinician_confirmed'", 1)
)
_TRIGGER = (
    "CREATE OR REPLACE TRIGGER ehr_longitudinal_binding BEFORE INSERT OR UPDATE\n"
)
SQL = (
    "SET LOCAL ROLE clinic_resolver;\n"
    + MEDICATION
    + "\n"
    + OBSERVATION
    + "\nREVOKE ALL ON FUNCTION clinic_app.ehr_medicationstatement_guard(),\n"
    " clinic_app.ehr_observation_guard() FROM PUBLIC;\n"
    "GRANT EXECUTE ON FUNCTION clinic_app.ehr_medicationstatement_guard(),\n"
    " clinic_app.ehr_observation_guard() TO clinic_owner;\n"
    "RESET ROLE;\n" + _TRIGGER + " ON clinic_app.ehr_medicationstatement FOR EACH ROW\n"
    " EXECUTE FUNCTION clinic_app.ehr_medicationstatement_guard();\n"
    + _TRIGGER
    + " ON clinic_app.ehr_observation FOR EACH ROW\n"
    " EXECUTE FUNCTION clinic_app.ehr_observation_guard();\n"
    "SET LOCAL ROLE clinic_resolver;\n"
    "REVOKE EXECUTE ON FUNCTION clinic_app.ehr_medicationstatement_guard(),\n"
    " clinic_app.ehr_observation_guard() FROM clinic_owner;\n"
    "DROP FUNCTION clinic_app.ehr_longitudinal_guard();\n"
    "RESET ROLE;\n"
)
REVERSE_SQL = (
    "SET LOCAL ROLE clinic_resolver;\n"
    + SHARED
    + "\nREVOKE ALL ON FUNCTION clinic_app.ehr_longitudinal_guard() FROM PUBLIC;\n"
    "GRANT EXECUTE ON FUNCTION clinic_app.ehr_longitudinal_guard() TO clinic_owner;\n"
    "RESET ROLE;\n" + _TRIGGER + " ON clinic_app.ehr_medicationstatement FOR EACH ROW\n"
    " EXECUTE FUNCTION clinic_app.ehr_longitudinal_guard();\n"
    + _TRIGGER
    + " ON clinic_app.ehr_observation FOR EACH ROW\n"
    " EXECUTE FUNCTION clinic_app.ehr_longitudinal_guard();\n"
    "SET LOCAL ROLE clinic_resolver;\n"
    "REVOKE EXECUTE ON FUNCTION clinic_app.ehr_longitudinal_guard()\n"
    " FROM clinic_owner;\n"
    "DROP FUNCTION clinic_app.ehr_medicationstatement_guard(),\n"
    " clinic_app.ehr_observation_guard();\n"
    "RESET ROLE;\n"
)
