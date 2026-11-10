"""Preserve the deployed workflow guard, adding only a bound review exception.

The base is the newest deployed definition, workflows 0008's coordination guard
(release notices and urgent concerns over care plans, data requests, retention
purge and orders). Rebuilding from an older text would drop those branches, so
every anchor must occur exactly once in the task branch; a moved anchor stops
the import instead of silently skipping the exception.
"""

from apps.workflows.migrations._coordination_sql import TASK_GUARD

_REPLACEMENTS = (
    (
        "IF TG_OP='INSERT' THEN\n"
        "   IF NOT clinic_app.has_permission('tasks.assign',NEW.clinic_id,NULL)",
        "IF TG_OP='INSERT' THEN\n"
        "   IF (NOT clinic_app.has_permission('tasks.assign',NEW.clinic_id,NULL) "
        "AND NOT clinic_app.enterprise_security_task(to_jsonb(NEW),'create'))",
    ),
    (
        "NOT clinic_app.workflows_owner_valid(NEW.clinic_id, NEW.owner_user_id,\n"
        "    NEW.owner_role)",
        "(NOT clinic_app.workflows_owner_valid(NEW.clinic_id, NEW.owner_user_id,\n"
        "    NEW.owner_role) AND NOT "
        "clinic_app.enterprise_security_task(to_jsonb(NEW),'review'))",
    ),
    (
        "NOT clinic_app.has_permission('tasks.complete',NEW.clinic_id,NULL)",
        "(NOT clinic_app.has_permission('tasks.complete',NEW.clinic_id,NULL) "
        "AND NOT clinic_app.enterprise_security_task(to_jsonb(NEW),'review'))",
    ),
    (
        "NOT clinic_app.workflows_owned("
        "OLD.clinic_id,OLD.owner_user_id,OLD.owner_role)",
        "(NOT clinic_app.workflows_owned("
        "OLD.clinic_id,OLD.owner_user_id,OLD.owner_role)"
        " AND NOT clinic_app.enterprise_security_task(to_jsonb(NEW),'review'))",
    ),
)
# The workflows 0008 deployment wraps the guard the same way (resolver-owned).
PRIOR_GUARD_SQL = "SET LOCAL ROLE clinic_resolver;\n" + TASK_GUARD + "\nRESET ROLE;\n"


def guard_sql() -> str:
    """Extend task branches only; all original identity and state checks remain."""
    task_end = TASK_GUARD.index(" ELSIF TG_TABLE_NAME='workflows_taskcomment'")
    task, rest = TASK_GUARD[:task_end], TASK_GUARD[task_end:]
    for anchor, replacement in _REPLACEMENTS:
        # The census decoder executes this module without str.count: split is
        # the exactly-once check (tests/scheduling/clock_runsql.py).
        if len(task.split(anchor)) - 1 != 1:
            message = "deployed workflow guard changed shape"
            raise RuntimeError(message)
        task = task.replace(anchor, replacement, 1)
    return "SET LOCAL ROLE clinic_resolver;\n" + task + rest + "\nRESET ROLE;\n"
