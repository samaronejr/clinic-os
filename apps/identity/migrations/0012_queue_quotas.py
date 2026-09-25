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

RESOLVER_SQL = """
SET LOCAL ROLE clinic_resolver;

CREATE FUNCTION clinic_app.identity_queue_quotas(
    requested_org pg_catalog.uuid
)
RETURNS pg_catalog.jsonb
LANGUAGE sql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp
AS $function$
    SELECT configuration.queue_quotas
    FROM clinic_app.identity_clinicconfiguration AS configuration
    WHERE configuration.organization_id = requested_org
    ORDER BY configuration.created_at DESC, configuration.id DESC
    LIMIT 1
$function$;

REVOKE ALL PRIVILEGES
    ON FUNCTION clinic_app.identity_queue_quotas(pg_catalog.uuid)
    FROM PUBLIC;
GRANT EXECUTE
    ON FUNCTION clinic_app.identity_queue_quotas(pg_catalog.uuid)
    TO clinic_app;

RESET ROLE;
"""

REVERSE_RESOLVER_SQL = """
SET LOCAL ROLE clinic_resolver;
DROP FUNCTION IF EXISTS clinic_app.identity_queue_quotas(pg_catalog.uuid);
RESET ROLE;
"""


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
        migrations.AddConstraint(
            model_name="clinicconfiguration",
            constraint=models.CheckConstraint(
                condition=models.expressions.RawSQL(
                    "jsonb_typeof(queue_quotas) = 'object' "
                    "AND queue_quotas - ARRAY['clinic-integrations','clinical',"
                    "'ai-interactive','ai-batch','messaging','finance','bulk']"
                    " = '{}'::jsonb "
                    "AND NOT jsonb_path_exists(queue_quotas, "
                    '\'strict $.* ? (@.type() != "number" || @.floor() != @ '
                    "|| @ < 1 || @ > 100000)')",
                    (),
                    output_field=models.BooleanField(),
                ),
                name="identity_config_queue_quotas_shape",
            ),
        ),
        migrations.RunSQL(RESOLVER_SQL, REVERSE_RESOLVER_SQL),
    ]
