"""Require current membership in every new scheduling and workflow authority."""

from collections.abc import Sequence
from typing import ClassVar

from django.db import migrations
from django.db.migrations.operations.base import Operation

from ._cut2_current_membership_sql import SQL


class Migration(migrations.Migration):
    """Run after the final scheduling body replay and workflow table creation."""

    dependencies: ClassVar[Sequence[tuple[str, str]]] = [
        ("identity", "0030_workflow_current_membership"),
        ("scheduling", "0006_capacity_insert_authorization"),
        ("workflows", "0004_task_completion_gate"),
    ]
    operations: ClassVar[Sequence[Operation]] = [migrations.RunSQL(SQL)]
