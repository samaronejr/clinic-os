"""Index only tenant HMAC snapshots, never encrypted identity predicates."""

from typing import ClassVar

from django.contrib.postgres.indexes import GinIndex
from django.db import migrations, models
from django.db.migrations.operations.base import Operation


class Migration(migrations.Migration):
    """Support purpose-bound same-tenant name/DOB and contact lookups."""

    dependencies: ClassVar = [("intake", "0022_alias_portal_reads")]
    operations: ClassVar[list[Operation]] = [
        migrations.AddIndex(
            model_name="patientduplicateindex",
            index=models.Index(
                fields=("organization", "name_birth_index"),
                name="intake_dup_name_birth_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="patientduplicateindex",
            index=GinIndex(fields=("contact_indexes",), name="intake_dup_contacts_gin"),
        ),
    ]
