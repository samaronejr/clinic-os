"""Preserve original financial and released-record lineage in unified reads."""

from typing import ClassVar

from django.db import migrations
from django.db.migrations.operations.base import Operation

from apps.billing.migrations._patient_alias_read_sql import SQL as BILLING_SQL
from apps.retention.migrations._patient_alias_read_sql import SQL as RETENTION_SQL


class Migration(migrations.Migration):
    """Change read predicates only; money, consent and record bindings stay."""

    dependencies: ClassVar = [
        ("intake", "0017_alias_clinical_reads"),
        ("billing", "0010_delegate_charges"),
        ("retention", "0004_delegate_confidentiality"),
    ]
    operations: ClassVar[list[Operation]] = [
        migrations.RunSQL(BILLING_SQL + RETENTION_SQL),
    ]
