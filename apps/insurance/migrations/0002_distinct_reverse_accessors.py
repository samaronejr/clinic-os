"""Give insurance's Batch and CommandReceipt reverse names distinct from inventory's.

Inventory and insurance both define clinic-scoped Batch and CommandReceipt
models, so their default reverse accessors on Clinic and Organization
clashed (fields.E304). Only Django state changes; no SQL is emitted.
"""

from typing import ClassVar

from django.db import migrations


class Migration(migrations.Migration):
    """Record the per-model default related names in migration state."""

    dependencies: ClassVar = [("insurance", "0001_revenue_cycle")]
    operations: ClassVar = [
        migrations.AlterModelOptions(
            name="batch", options={"default_related_name": "insurance_batches"}
        ),
        migrations.AlterModelOptions(
            name="commandreceipt",
            options={"default_related_name": "insurance_command_receipts"},
        ),
    ]
