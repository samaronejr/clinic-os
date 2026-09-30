"""Store per-user display preferences, readable and editable only by the user."""

from collections.abc import Sequence
from typing import ClassVar, Final

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.migrations.operations.base import Operation

FORWARD_SQL: Final = """
ALTER TABLE clinic_app.identity_userpreference OWNER TO clinic_owner;
ALTER TABLE clinic_app.identity_userpreference ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.identity_userpreference FORCE ROW LEVEL SECURITY;

CREATE POLICY userpreference_owner_only
    ON clinic_app.identity_userpreference
    AS PERMISSIVE
    FOR ALL
    TO clinic_app
    USING (
        user_id = NULLIF(
            pg_catalog.current_setting('app.current_user_id', true),
            ''
        )::pg_catalog.uuid
    )
    WITH CHECK (
        user_id = NULLIF(
            pg_catalog.current_setting('app.current_user_id', true),
            ''
        )::pg_catalog.uuid
    );

REVOKE ALL PRIVILEGES
    ON TABLE clinic_app.identity_userpreference
    FROM PUBLIC, clinic_app, clinic_resolver;
GRANT SELECT, INSERT ON TABLE clinic_app.identity_userpreference TO clinic_app;
GRANT UPDATE (theme, density, updated_at)
    ON TABLE clinic_app.identity_userpreference
    TO clinic_app;
"""

REVERSE_SQL: Final = """
DROP POLICY IF EXISTS userpreference_owner_only
    ON clinic_app.identity_userpreference;
"""


class Migration(migrations.Migration):
    """Create the table, then bind every row to the transaction's user GUC."""

    dependencies: ClassVar[Sequence[tuple[str, str]]] = [
        ("identity", "0010_configuration_reminder_schedule"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations: ClassVar[Sequence[Operation]] = [
        migrations.CreateModel(
            name="UserPreference",
            fields=[
                (
                    "user",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        primary_key=True,
                        serialize=False,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "theme",
                    models.CharField(
                        choices=[("light", "Light"), ("dark", "Dark")],
                        default="light",
                        max_length=16,
                    ),
                ),
                (
                    "density",
                    models.CharField(
                        choices=[
                            ("comfortable", "Comfortable"),
                            ("compact", "Compact"),
                        ],
                        default="comfortable",
                        max_length=16,
                    ),
                ),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(
                            ("theme__in", ("light", "dark")),
                            ("density__in", ("comfortable", "compact")),
                        ),
                        name="identity_userpreference_closed_values",
                    )
                ],
            },
        ),
        migrations.RunSQL(sql=FORWARD_SQL, reverse_sql=REVERSE_SQL),
    ]
