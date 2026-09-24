"""Install the bounded pending-operation recovery scan.

`enqueue_operation` publishes to the broker inside ``transaction.on_commit``;
a broker outage at that instant leaves the committed row pending forever.
This additive migration installs ``comms_recover_pending_v1`` — the same
claimable-shape scan as ``comms_due_reminders_v1`` but subject-agnostic and
NULL-``not_before`` aware — so the periodic recovery task can re-dispatch
stuck work for every subject type.
"""

from collections.abc import Sequence
from typing import ClassVar

from django.db import migrations
from django.db.migrations.operations.base import Operation

from apps.comms.migrations._recovery_sql import REVERSE_SQL, SQL


class Migration(migrations.Migration):
    """Additive resolver function; no schema or grant shape changes."""

    dependencies: ClassVar[Sequence[tuple[str, str]]] = [
        ("comms", "0003_video_channel"),
    ]

    operations: ClassVar[Sequence[Operation]] = [
        migrations.RunSQL(SQL, reverse_sql=REVERSE_SQL),
    ]
