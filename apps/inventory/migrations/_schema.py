"""Inventory protected schema: irreversible; rollback = restore."""

import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.migrations.operations.base import Operation

import apps.tenancy.fields

MODELS: list[Operation] = [
    migrations.CreateModel(
        name="Supplier",
        fields=[
            (
                "organization",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    to="identity.organization",
                ),
            ),
            (
                "id",
                models.UUIDField(
                    default=uuid.uuid4,
                    editable=False,
                    primary_key=True,
                    serialize=False,
                ),
            ),
            (
                "clinic",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, to="identity.clinic"
                ),
            ),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            (
                "name",
                apps.tenancy.fields.EncryptedTextField(
                    purpose="inventory.supplier.name"
                ),
            ),
            (
                "contact",
                apps.tenancy.fields.EncryptedTextField(
                    null=True, purpose="inventory.supplier.contact"
                ),
            ),
        ],
        options={
            "abstract": False,
            "indexes": [],
            "constraints": [],
        },
    ),
    migrations.CreateModel(
        name="Item",
        fields=[
            (
                "organization",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    to="identity.organization",
                ),
            ),
            (
                "id",
                models.UUIDField(
                    default=uuid.uuid4,
                    editable=False,
                    primary_key=True,
                    serialize=False,
                ),
            ),
            (
                "clinic",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, to="identity.clinic"
                ),
            ),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            ("name", models.CharField(max_length=160)),
            ("unit", models.CharField(default="each", max_length=24)),
        ],
        options={
            "abstract": False,
            "indexes": [],
            "constraints": [],
        },
    ),
    migrations.CreateModel(
        name="Batch",
        fields=[
            (
                "organization",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    to="identity.organization",
                ),
            ),
            (
                "id",
                models.UUIDField(
                    default=uuid.uuid4,
                    editable=False,
                    primary_key=True,
                    serialize=False,
                ),
            ),
            (
                "clinic",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, to="identity.clinic"
                ),
            ),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            (
                "item",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, to="inventory.item"
                ),
            ),
            ("lot", models.CharField(max_length=120)),
            ("expires_on", models.DateField()),
        ],
        options={
            "constraints": [
                models.UniqueConstraint(
                    fields=("clinic", "item", "lot"), name="inventory_batch_lot"
                )
            ],
            "indexes": [],
        },
    ),
    migrations.CreateModel(
        name="Location",
        fields=[
            (
                "organization",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    to="identity.organization",
                ),
            ),
            (
                "id",
                models.UUIDField(
                    default=uuid.uuid4,
                    editable=False,
                    primary_key=True,
                    serialize=False,
                ),
            ),
            (
                "clinic",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, to="identity.clinic"
                ),
            ),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            ("name", models.CharField(max_length=160)),
        ],
        options={
            "abstract": False,
            "indexes": [],
            "constraints": [],
        },
    ),
    migrations.CreateModel(
        name="MinimumStock",
        fields=[
            (
                "organization",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    to="identity.organization",
                ),
            ),
            (
                "id",
                models.UUIDField(
                    default=uuid.uuid4,
                    editable=False,
                    primary_key=True,
                    serialize=False,
                ),
            ),
            (
                "clinic",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, to="identity.clinic"
                ),
            ),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            (
                "item",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, to="inventory.item"
                ),
            ),
            (
                "location",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, to="inventory.location"
                ),
            ),
            ("quantity", models.PositiveIntegerField()),
        ],
        options={
            "abstract": False,
            "indexes": [],
            "constraints": [],
        },
    ),
    migrations.CreateModel(
        name="PurchaseBudget",
        fields=[
            (
                "organization",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    to="identity.organization",
                ),
            ),
            (
                "id",
                models.UUIDField(
                    default=uuid.uuid4,
                    editable=False,
                    primary_key=True,
                    serialize=False,
                ),
            ),
            (
                "clinic",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, to="identity.clinic"
                ),
            ),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            ("command_id", models.UUIDField(default=uuid.uuid4)),
            ("amount_minor", models.PositiveBigIntegerField()),
            (
                "approved_by",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="+",
                    to=settings.AUTH_USER_MODEL,
                ),
            ),
            ("approved_at", models.DateTimeField()),
        ],
        options={
            "abstract": False,
            "constraints": [
                models.UniqueConstraint(
                    fields=("clinic", "command_id"),
                    name="purchasebudget_inventory_command",
                )
            ],
            "indexes": [],
        },
    ),
    migrations.CreateModel(
        name="PurchaseRequest",
        fields=[
            (
                "organization",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    to="identity.organization",
                ),
            ),
            (
                "id",
                models.UUIDField(
                    default=uuid.uuid4,
                    editable=False,
                    primary_key=True,
                    serialize=False,
                ),
            ),
            (
                "clinic",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, to="identity.clinic"
                ),
            ),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            ("command_id", models.UUIDField(default=uuid.uuid4)),
            (
                "item",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, to="inventory.item"
                ),
            ),
            (
                "supplier",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, to="inventory.supplier"
                ),
            ),
            (
                "budget",
                models.ForeignKey(
                    null=True,
                    on_delete=django.db.models.deletion.PROTECT,
                    to="inventory.purchasebudget",
                ),
            ),
            ("quantity", models.PositiveIntegerField()),
            ("amount_minor", models.PositiveBigIntegerField()),
            ("state", models.CharField(default="draft", max_length=16)),
            (
                "approved_by",
                models.ForeignKey(
                    null=True,
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="+",
                    to=settings.AUTH_USER_MODEL,
                ),
            ),
            ("approved_at", models.DateTimeField(null=True)),
            ("last_command_id", models.UUIDField(null=True)),
        ],
        options={
            "abstract": False,
            "constraints": [
                models.UniqueConstraint(
                    fields=("clinic", "command_id"),
                    name="purchaserequest_inventory_command",
                )
            ],
            "indexes": [],
        },
    ),
    migrations.CreateModel(
        name="StockMovement",
        fields=[
            (
                "organization",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    to="identity.organization",
                ),
            ),
            (
                "id",
                models.UUIDField(
                    default=uuid.uuid4,
                    editable=False,
                    primary_key=True,
                    serialize=False,
                ),
            ),
            (
                "clinic",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, to="identity.clinic"
                ),
            ),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            ("command_id", models.UUIDField(default=uuid.uuid4)),
            (
                "batch",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, to="inventory.batch"
                ),
            ),
            (
                "location",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, to="inventory.location"
                ),
            ),
            (
                "destination_location",
                models.ForeignKey(
                    null=True,
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="+",
                    to="inventory.location",
                ),
            ),
            ("kind", models.CharField(max_length=16)),
            ("quantity", models.PositiveIntegerField()),
            ("reason", models.CharField(blank=True, default="", max_length=24)),
            (
                "source_movement",
                models.ForeignKey(
                    null=True,
                    on_delete=django.db.models.deletion.PROTECT,
                    to="inventory.stockmovement",
                ),
            ),
            (
                "procedure_entry",
                models.ForeignKey(
                    null=True,
                    on_delete=django.db.models.deletion.PROTECT,
                    to="ehr.specialtyentry",
                ),
            ),
            (
                "purchase_request",
                models.OneToOneField(
                    null=True,
                    on_delete=django.db.models.deletion.PROTECT,
                    to="inventory.purchaserequest",
                ),
            ),
            (
                "recorded_by",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="+",
                    to=settings.AUTH_USER_MODEL,
                ),
            ),
        ],
        options={
            "abstract": False,
            "constraints": [
                models.UniqueConstraint(
                    fields=("clinic", "command_id"),
                    name="stockmovement_inventory_command",
                )
            ],
            "indexes": [],
        },
    ),
    migrations.CreateModel(
        name="Equipment",
        fields=[
            (
                "organization",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    to="identity.organization",
                ),
            ),
            (
                "id",
                models.UUIDField(
                    default=uuid.uuid4,
                    editable=False,
                    primary_key=True,
                    serialize=False,
                ),
            ),
            (
                "clinic",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, to="identity.clinic"
                ),
            ),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            ("command_id", models.UUIDField(default=uuid.uuid4)),
            ("name", models.CharField(max_length=160)),
            ("serial", models.CharField(max_length=120)),
            (
                "supplier",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, to="inventory.supplier"
                ),
            ),
            (
                "location",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, to="inventory.location"
                ),
            ),
        ],
        options={
            "abstract": False,
            "constraints": [
                models.UniqueConstraint(
                    fields=("clinic", "command_id"), name="equipment_inventory_command"
                )
            ],
            "indexes": [],
        },
    ),
    migrations.CreateModel(
        name="Calibration",
        fields=[
            (
                "organization",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    to="identity.organization",
                ),
            ),
            (
                "id",
                models.UUIDField(
                    default=uuid.uuid4,
                    editable=False,
                    primary_key=True,
                    serialize=False,
                ),
            ),
            (
                "clinic",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, to="identity.clinic"
                ),
            ),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            ("command_id", models.UUIDField(default=uuid.uuid4)),
            (
                "equipment",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    to="inventory.equipment",
                ),
            ),
            ("performed_on", models.DateField()),
            ("due_on", models.DateField()),
            ("outcome", models.CharField(max_length=16)),
        ],
        options={
            "abstract": False,
            "constraints": [
                models.UniqueConstraint(
                    fields=("clinic", "command_id"),
                    name="calibration_inventory_command",
                )
            ],
            "indexes": [],
        },
    ),
    migrations.CreateModel(
        name="Maintenance",
        fields=[
            (
                "organization",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    to="identity.organization",
                ),
            ),
            (
                "id",
                models.UUIDField(
                    default=uuid.uuid4,
                    editable=False,
                    primary_key=True,
                    serialize=False,
                ),
            ),
            (
                "clinic",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, to="identity.clinic"
                ),
            ),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            ("command_id", models.UUIDField(default=uuid.uuid4)),
            (
                "equipment",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    to="inventory.equipment",
                ),
            ),
            ("performed_on", models.DateField()),
            ("due_on", models.DateField()),
            ("outcome", models.CharField(max_length=16)),
        ],
        options={
            "abstract": False,
            "constraints": [
                models.UniqueConstraint(
                    fields=("clinic", "command_id"),
                    name="maintenance_inventory_command",
                )
            ],
            "indexes": [],
        },
    ),
    migrations.CreateModel(
        name="CommandReceipt",
        fields=[
            (
                "organization",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    to="identity.organization",
                ),
            ),
            (
                "id",
                models.UUIDField(
                    default=uuid.uuid4,
                    editable=False,
                    primary_key=True,
                    serialize=False,
                ),
            ),
            (
                "clinic",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT, to="identity.clinic"
                ),
            ),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            ("command_id", models.UUIDField()),
            ("operation", models.CharField(max_length=32)),
            ("fingerprint", models.CharField(max_length=64)),
            ("object_type", models.CharField(max_length=24)),
            ("object_id", models.UUIDField()),
            (
                "recorded_by",
                models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name="+",
                    to=settings.AUTH_USER_MODEL,
                ),
            ),
        ],
        options={
            "constraints": [
                models.UniqueConstraint(
                    fields=("clinic", "command_id"), name="inventory_receipt_command"
                ),
                models.CheckConstraint(
                    condition=models.Q(("fingerprint__regex", "^[0-9a-f]{64}$")),
                    name="inventory_receipt_digest",
                ),
            ],
            "indexes": [],
        },
    ),
]


class ProtectedSchema(migrations.SeparateDatabaseAndState):
    """New encrypted fields are irreversible; rollback requires restore."""

    atomic = True
    reversible = False
