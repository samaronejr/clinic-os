"""Exact clinic charges and append-only financial lineage, without clinical text."""

from __future__ import annotations

import uuid
from typing import ClassVar

from django.db import models
from django.utils import timezone

from apps.tenancy.fields import EncryptedTextField
from apps.tenancy.models import TenantScopedModel


class Invoice(TenantScopedModel):
    """A stable charge identity whose financial terms freeze on issue."""

    class State(models.TextChoices):
        """Only confirmed settlement can enter paid."""

        DRAFT = "draft", "Draft"
        OPEN = "open", "Open"
        PAID = "paid", "Paid"
        CANCELLED = "cancelled", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    patient = models.ForeignKey("intake.Patient", on_delete=models.PROTECT)
    appointment = models.ForeignKey(
        "scheduling.Appointment", on_delete=models.PROTECT, null=True, blank=True
    )
    encounter = models.ForeignKey(
        "ehr.Encounter", on_delete=models.PROTECT, null=True, blank=True
    )
    reference = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    amount_minor = models.PositiveBigIntegerField()
    currency = models.CharField(max_length=3, default="BRL")
    state = models.CharField(max_length=16, choices=State, default=State.DRAFT)
    revision = models.PositiveIntegerField(default=1)
    idempotency_key = models.UUIDField()
    create_fingerprint = models.BinaryField(max_length=32, editable=False)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    issued_at = models.DateTimeField(null=True, blank=True)
    released_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        """Reject unsupported currency, incoherent state and replayed creates."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("organization", "idempotency_key"),
                name="billing_invoice_org_idempotency_uniq",
            ),
            models.CheckConstraint(
                condition=models.Q(amount_minor__gt=0, currency="BRL", revision__gte=1),
                name="billing_invoice_terms",
            ),
            models.CheckConstraint(
                condition=models.Q(state="draft", issued_at__isnull=True)
                | models.Q(state__in=["open", "paid"], issued_at__isnull=False)
                | models.Q(state="cancelled"),
                name="billing_invoice_state",
            ),
            models.CheckConstraint(
                condition=models.Q(released_at__isnull=True)
                | models.Q(issued_at__isnull=False),
                name="billing_invoice_release",
            ),
        ]


class InvoiceRevision(TenantScopedModel):
    """Database-owned snapshot of every draft revision, including its initial terms."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT)
    revision = models.PositiveIntegerField()
    amount_minor = models.PositiveBigIntegerField()
    currency = models.CharField(max_length=3)
    reference = models.UUIDField()
    actor_id = models.UUIDField()
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        """A revision number identifies exactly one retained snapshot."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("invoice", "revision"), name="billing_revision_unique"
            ),
        ]


class Settlement(TenantScopedModel):
    """A staff-confirmed manual settlement, not an unverified provider callback.

    The opaque confirmation reference identifies externally retained evidence.
    Provider authentication and reconciliation belong to their subsequent task.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    invoice = models.OneToOneField(Invoice, on_delete=models.PROTECT)
    confirmation_reference = models.UUIDField()
    amount_minor = models.PositiveBigIntegerField()
    currency = models.CharField(max_length=3, default="BRL")
    confirmed_by_id = models.UUIDField()
    confirmed_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        """One confirmation cannot settle multiple charges in an organization."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("organization", "confirmation_reference"),
                name="billing_confirmation_unique",
            ),
        ]


class Receipt(TenantScopedModel):
    """Database-derived immutable receipt of exactly one confirmed settlement."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    settlement = models.OneToOneField(Settlement, on_delete=models.PROTECT)
    invoice = models.OneToOneField(Invoice, on_delete=models.PROTECT)
    reference = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    amount_minor = models.PositiveBigIntegerField()
    currency = models.CharField(max_length=3)
    issued_at = models.DateTimeField(default=timezone.now, editable=False)


class PixOperation(TenantScopedModel):
    """Immutable exact invoice request; expiration requires a linked successor."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT)
    previous = models.OneToOneField(
        "self", on_delete=models.PROTECT, null=True, blank=True
    )
    invoice_reference = models.UUIDField()
    amount_minor = models.PositiveBigIntegerField()
    currency = models.CharField(max_length=3)
    provider = models.CharField(max_length=64, default="synthetic-pix-v1")
    actor_id = models.UUIDField()
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    expires_at = models.DateTimeField()

    class Meta:
        """One root per invoice and one successor per expired operation."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("invoice",),
                condition=models.Q(previous__isnull=True),
                name="billing_pix_one_root",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    amount_minor__gt=0, currency="BRL", provider="synthetic-pix-v1"
                )
                & models.Q(expires_at__gt=models.F("created_at")),
                name="billing_pix_terms",
            ),
        ]


class PixCharge(TenantScopedModel):
    """Immutable verified synthetic result, never settlement evidence."""

    operation = models.OneToOneField(
        PixOperation, primary_key=True, on_delete=models.PROTECT
    )
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT)
    provider_reference = models.CharField(max_length=128, unique=True)
    copy_code = EncryptedTextField(purpose="billing.pixcharge.copy_code")
    qr_base64 = EncryptedTextField(purpose="billing.pixcharge.qr_base64")
    synthetic = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        """The closed provider gate cannot be bypassed through result flags."""

        constraints: ClassVar = [
            models.CheckConstraint(
                condition=models.Q(synthetic=True), name="billing_pix_synthetic"
            ),
        ]


class PaymentEvent(TenantScopedModel):
    """Immutable record of one authenticated provider payment event.

    The stored charge operation, never event payload claims, resolves the
    tenant, clinic, invoice and recorded actor. ``resolution`` is the explicit
    outcome: ``settled`` events link exactly one settlement, while every
    discrepancy stays ``operator_required`` with a fixed reason vocabulary.
    """

    class ReportedStatus(models.TextChoices):
        """Provider-reported charge states accepted after authentication."""

        PENDING = "pending", "Pending"
        SETTLED = "settled", "Settled"
        EXPIRED = "expired", "Expired"
        CANCELLED = "cancelled", "Cancelled"
        REVERSED = "reversed", "Reversed"

    class Resolution(models.TextChoices):
        """Explicit reconciliation outcome for one authenticated event."""

        SETTLED = "settled", "Settled"
        RECORDED = "recorded", "Recorded"
        OPERATOR_REQUIRED = "operator_required", "Operator required"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    operation = models.ForeignKey(PixOperation, on_delete=models.PROTECT)
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT)
    settlement = models.ForeignKey(
        Settlement, on_delete=models.PROTECT, null=True, blank=True
    )
    provider = models.CharField(max_length=64)
    event_id = models.CharField(max_length=255)
    provider_reference = models.CharField(max_length=128)
    actor_id = models.UUIDField()
    reported_status = models.CharField(max_length=16, choices=ReportedStatus)
    reported_amount_minor = models.PositiveBigIntegerField()
    reported_currency = models.CharField(max_length=3)
    authoritative_status = models.CharField(  # noqa: DJ001 - NULL marks an
        max_length=16, choices=ReportedStatus, null=True, blank=True
    )  # unverifiable lookup, distinct from any reported status.
    authoritative_amount_minor = models.PositiveBigIntegerField(null=True, blank=True)
    authoritative_currency = models.CharField(  # noqa: DJ001 - paired with
        max_length=3, null=True, blank=True
    )  # authoritative_status.
    resolution = models.CharField(max_length=24, choices=Resolution)
    reason_code = models.CharField(  # noqa: DJ001 - NULL only on settled rows
        max_length=64, null=True, blank=True
    )
    received_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        """Deduplicate provider event ids and keep resolutions coherent."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("provider", "event_id"),
                name="billing_payment_event_dedupe",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    resolution="settled",
                    reason_code__isnull=True,
                    settlement__isnull=False,
                )
                | models.Q(
                    resolution__in=("recorded", "operator_required"),
                    reason_code__isnull=False,
                    settlement__isnull=True,
                ),
                name="billing_payment_event_resolution",
            ),
        ]
