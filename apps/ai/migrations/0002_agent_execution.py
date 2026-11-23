"""Create protected action storage; irreversible, rollback is restore."""

from typing import ClassVar

from django.db import migrations

from apps.ai.migrations import (
    _agent_access_sql,
    _agent_audit_sql,
    _agent_guard_sql,
    _agent_posture_sql,
)
from apps.ai.migrations._agent_tables import OPERATIONS
from apps.ai.migrations._state_batch import CheckpointedStateBatch
from apps.tenancy.migrations._resumable_schema import ResumableRunSQL


class Migration(migrations.Migration):
    """Install every grant, scope key and guard in the creating migration."""

    atomic = False
    dependencies: ClassVar = [
        ("ai", "0001_initial"),
        ("identity", "0040_agent_execution_authority"),
        ("workflows", "0004_task_completion_gate"),
        ("providers", "0001_initial"),
    ]
    operations: ClassVar = [
        CheckpointedStateBatch(
            receipt="ai_compensation_organization_id_0609c461",
            database_operations=OPERATIONS,
            state_operations=OPERATIONS,
        ),
        ResumableRunSQL(
            "BEGIN;\n"
            + _agent_access_sql.SQL
            + _agent_posture_sql.SQL
            + _agent_guard_sql.SQL
            + _agent_audit_sql.SQL
            + "\nCOMMIT;",
            table="ai_agentrun",
            trigger="agent_record_guard",
        ),
    ]
