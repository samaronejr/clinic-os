"""Add the organization-consistent clinic role foreign key."""

from typing import ClassVar

from django.db import migrations
from django.db.migrations.operations.base import Operation


class Migration(migrations.Migration):
    """Install the reversible PostgreSQL composite foreign key."""

    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("identity", "0001_initial"),
    ]

    operations: ClassVar[list[Operation]] = [
        migrations.RunSQL(
            sql=(
                'ALTER TABLE "identity_userclinicrole" '
                'ADD CONSTRAINT "identity_userclinicrole_org_clinic_fk" '
                'FOREIGN KEY ("organization_id", "clinic_id") '
                'REFERENCES "identity_clinic" ("organization_id", "id") '
                "DEFERRABLE INITIALLY DEFERRED"
            ),
            reverse_sql=(
                'ALTER TABLE "identity_userclinicrole" '
                'DROP CONSTRAINT "identity_userclinicrole_org_clinic_fk"'
            ),
        )
    ]
