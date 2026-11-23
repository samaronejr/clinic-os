"""Per-organization queue quotas on the bounded configuration snapshot.

``queue_quotas`` is a validated JSON map from queue name to admitted tasks
per minute; the CHECK constraint mirrors the service validator so a forged
map cannot be stored even through a raw insert. The
``identity_queue_quotas`` resolver lets workers read the latest published
map for an organization without a request actor.
"""

from collections.abc import Sequence
from typing import ClassVar

from django.db import migrations, models
from django.db.migrations.operations.base import Operation

from apps.identity.migrations._queue_quotas_sql import (
    RESOLVER_SQL,
    REVERSE_RESOLVER_SQL,
)
from apps.tenancy.migration_operations import AddConstraint


class Migration(migrations.Migration):
    """Add the validated quota map and its worker-facing resolver."""

    dependencies: ClassVar[Sequence[tuple[str, str]]] = [
        ("identity", "0011_user_preference"),
    ]

    operations: ClassVar[Sequence[Operation]] = [
        migrations.AddField(
            model_name="clinicconfiguration",
            name="queue_quotas",
            field=models.JSONField(blank=True, default=dict),
        ),
        AddConstraint(
            model_name="clinicconfiguration",
            constraint=models.CheckConstraint(
                condition=models.expressions.RawSQL(
                    # The canonical text clause rejects integral floats
                    # ("60.0") that jsonb keeps lexically; "1e2" is
                    # normalized by jsonb to 100 and reads back as an int.
                    "jsonb_typeof(queue_quotas) = 'object' "
                    "AND queue_quotas - ARRAY['clinic-integrations','clinical',"
                    "'ai-interactive','ai-batch','messaging','finance','bulk']"
                    " = '{}'::jsonb "
                    "AND queue_quotas::text ~ "
                    '\'^\\{("[a-z-]+": [0-9]+(, "[a-z-]+": [0-9]+)*)?\\}$\' '
                    "AND NOT jsonb_path_exists(queue_quotas, "
                    '\'strict $.* ? (@.type() != "number" '
                    "|| @ < 1 || @ > 100000)')",
                    (),
                    output_field=models.BooleanField(),
                ),
                name="identity_config_queue_quotas_shape",
            ),
        ),
        migrations.RunSQL(RESOLVER_SQL, REVERSE_RESOLVER_SQL),
    ]
