"""Admit the front-desk agent's outbound replies in the message contract (todo 54).

An outbound row may now carry no sender; the command id stays required (the
agent's turn id) and the message guard admits such a row only from the agent's
finish bridge (``apps/ai/migrations/_front_desk_sql.py``). Metadata only: no
existing row changes, and the reverse restores the stricter contract on a
database that holds no agent reply (once one exists, rollback = restore).
"""

from collections.abc import Sequence
from typing import ClassVar

from django.db import migrations, models
from django.db.migrations.operations.base import Operation

from apps.ai.migrations._state_batch import SchemaStateBatch

_PROVIDER_STATES = ["sent", "delivered", "read", "failed"]


def _contract() -> models.CheckConstraint:
    return models.CheckConstraint(
        condition=(
            models.Q(direction="out", command_id__isnull=False, event_id__isnull=True)
            | models.Q(
                direction="in",
                sent_by__isnull=True,
                command_id__isnull=True,
                event_id__isnull=False,
                template__isnull=True,
            )
        )
        & (models.Q(provider_state="") | models.Q(provider_state__in=_PROVIDER_STATES)),
        name="comms_message_contract",
    )


_CONTRACT: list[Operation] = [
    migrations.RemoveConstraint(model_name="message", name="comms_message_contract"),
    migrations.AddConstraint(model_name="message", constraint=_contract()),
]


class Migration(migrations.Migration):
    """One constraint, replaced in place."""

    dependencies: ClassVar[Sequence[tuple[str, str]]] = [
        ("comms", "0010_portal_messages"),
    ]
    # One batch renders the message model once instead of the tenant graph twice.
    operations: ClassVar[Sequence[Operation]] = [
        SchemaStateBatch(
            database_operations=list(_CONTRACT), state_operations=list(_CONTRACT)
        ),
    ]
