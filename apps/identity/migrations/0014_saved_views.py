"""Store per-user saved workspace views, readable and archivable only by the user.

A row is visible only to its user, in the current tenant, while the user still
holds a role in the row's clinic. The runtime role inserts and archives (the
``archived_at`` column) and never deletes; parameters are closed slugs.
"""

import uuid
from collections.abc import Sequence
from typing import ClassVar, Final

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.migrations.operations.base import Operation

from apps.tenancy.migrations_support import revoke_default_dml

_OWNER: Final = """
        organization_id = NULLIF(
            pg_catalog.current_setting('app.current_tenant', true), ''
        )::pg_catalog.uuid
        AND user_id = NULLIF(
            pg_catalog.current_setting('app.current_user_id', true), ''
        )::pg_catalog.uuid
        AND EXISTS (
            SELECT 1 FROM clinic_app.identity_userclinicrole AS role_row
             WHERE role_row.user_id = identity_savedview.user_id
               AND role_row.clinic_id = identity_savedview.clinic_id
               AND role_row.organization_id = identity_savedview.organization_id
        )
"""

FORWARD_SQL: Final = (
    """
ALTER TABLE clinic_app.identity_savedview OWNER TO clinic_owner;
ALTER TABLE clinic_app.identity_savedview ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.identity_savedview FORCE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.identity_savedview
    ADD CONSTRAINT identity_savedview_clinic_fk
    FOREIGN KEY (organization_id, clinic_id)
    REFERENCES clinic_app.identity_clinic (organization_id, id);

CREATE POLICY savedview_owner_only
    ON clinic_app.identity_savedview
    AS PERMISSIVE
    FOR ALL
    TO clinic_app
    USING ("""
    + _OWNER
    + """    )
    WITH CHECK ("""
    + _OWNER
    + """    );
"""
    + revoke_default_dml("identity_savedview", {"SELECT", "INSERT"})
    + """
REVOKE ALL PRIVILEGES ON TABLE clinic_app.identity_savedview
    FROM PUBLIC, clinic_resolver;
GRANT UPDATE (archived_at) ON TABLE clinic_app.identity_savedview TO clinic_app;
"""
)

REVERSE_SQL: Final = """
DROP POLICY IF EXISTS savedview_owner_only ON clinic_app.identity_savedview;
ALTER TABLE clinic_app.identity_savedview
    DROP CONSTRAINT IF EXISTS identity_savedview_clinic_fk;
"""


class Migration(migrations.Migration):
    """Create the table, then bind every row to its user, tenant and clinic role."""

    dependencies: ClassVar[Sequence[tuple[str, str]]] = [
        ("identity", "0013_permission_bundles"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations: ClassVar[Sequence[Operation]] = [
        migrations.CreateModel(
            name="SavedView",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("destination", models.CharField(max_length=32)),
                ("params", models.JSONField(default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("archived_at", models.DateTimeField(blank=True, null=True)),
                (
                    "clinic",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="identity.clinic",
                    ),
                ),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="identity.organization",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(
                            ("destination__regex", "^[a-z][a-z-]{0,31}$")
                        ),
                        name="identity_savedview_destination_slug",
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(("archived_at__isnull", True)),
                        fields=("user", "clinic", "destination", "params"),
                        name="identity_savedview_active_uniq",
                    ),
                ],
            },
        ),
        migrations.RunSQL(sql=FORWARD_SQL, reverse_sql=REVERSE_SQL),
    ]
