"""Install the aggregate outbox-state resolver for the metrics scrape.

Todo 11's ``/internal/metrics`` endpoint is sessionless (D-19), so no tenant
GUC exists and RLS hides all outbox rows. ``comms_operation_state_counts_v1``
returns only per-status aggregates — count and oldest ``created_at`` — which
is exactly the SLI shape the endpoint needs; row content stays unreachable.
"""

from collections.abc import Sequence
from typing import ClassVar

from django.db import migrations
from django.db.migrations.operations.base import Operation

from apps.comms.migrations._metrics_sql import REVERSE_SQL, SQL


class Migration(migrations.Migration):
    """Additive resolver function; no schema or grant shape changes."""

    dependencies: ClassVar[Sequence[tuple[str, str]]] = [
        ("comms", "0004_pending_recovery"),
    ]

    operations: ClassVar[Sequence[Operation]] = [
        migrations.RunSQL(SQL, reverse_sql=REVERSE_SQL),
    ]
