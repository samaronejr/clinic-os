"""Move service INSERT authority ahead of capacity-only early returns."""

from apps.scheduling.migrations._resources_sql import GUARDS_SQL

_HEADER = "CREATE FUNCTION clinic_app.scheduling_capacity_guard()"
_PREVIOUS = (
    _HEADER
    + GUARDS_SQL.split(_HEADER, 1)[1].split(
        "REVOKE ALL ON FUNCTION clinic_app.scheduling_definition_guard()", 1
    )[0]
)
_INSERT_CHECK = """
    IF NOT (clinic_app.has_permission('appointment.book',NEW.clinic_id,NULL)
     OR (clinic_app.has_permission('appointment.book_own',NEW.clinic_id,NULL)
         AND NEW.practitioner_id=NULLIF(current_setting('app.current_user_id',true),
 '')::uuid)) THEN
     RAISE EXCEPTION 'scheduling access denied' USING ERRCODE='42501';
    END IF;
"""
_EARLY_RETURN = "  IF NOT consumes THEN RETURN NEW; END IF;"
_AUTHORIZED = _PREVIOUS.replace(_INSERT_CHECK, "\n", 1).replace(
    _EARLY_RETURN,
    "  IF TG_OP='INSERT' AND NEW.service_type_id IS NOT NULL THEN"
    + _INSERT_CHECK
    + "  END IF;\n"
    + _EARLY_RETURN,
    1,
)

SQL = (
    "SET LOCAL ROLE clinic_resolver;\n"
    + _AUTHORIZED.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)
    + "RESET ROLE;"
)
REVERSE_SQL = (
    "SET LOCAL ROLE clinic_resolver;\n"
    + _PREVIOUS.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)
    + "RESET ROLE;"
)
