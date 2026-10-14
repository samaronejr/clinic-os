"""Preserve historical consent and released-document bindings in portal reads."""

from typing import ClassVar

from django.db import migrations
from django.db.migrations.operations.base import Operation

from apps.intake.migrations._alias_portal_sql import SQL
from apps.prescription.migrations._patient_alias_read_sql import SQL as DOCUMENT_SQL


class Migration(migrations.Migration):
    """Expand read lineage only; write, release and revocation authority stay exact."""

    dependencies: ClassVar = [
        ("intake", "0019_merge_transfer_bindings"),
        ("consent", "0005_delegate_purposes"),
        ("prescription", "0011_delegate_confidentiality"),
    ]
    operations: ClassVar[list[Operation]] = [migrations.RunSQL(SQL + DOCUMENT_SQL)]
