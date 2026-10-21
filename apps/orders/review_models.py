"""Independent matching, exception, review and audience receipts."""

from __future__ import annotations

from typing import ClassVar

from django.conf import settings
from django.db import models

from apps.orders.order_models import ScopedRecord


class ResultMatch(ScopedRecord):
    """A checked exact identity binds one source version to one order."""

    version = models.OneToOneField("orders.ResultVersion", on_delete=models.PROTECT)
    order = models.ForeignKey("orders.Order", on_delete=models.PROTECT)
    factors = models.JSONField()
    operator = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT
    )
    evidence_code = models.CharField(max_length=32, default="source_identity_verified")


class ResultQuarantine(ScopedRecord):
    """Quarantined bytes never participate in patient chart queries."""

    version = models.OneToOneField("orders.ResultVersion", on_delete=models.PROTECT)
    reason = models.CharField(max_length=24)

    @property
    def work_item(self) -> ResultWorkItem:
        """Resolve the DB-created exception task without a mutable backlink."""
        return ResultWorkItem.objects.get(version=self.version, kind="quarantine")


class ResultRejection(ScopedRecord):
    """Operator evidence can reject, never force-file, an identity mismatch."""

    version = models.OneToOneField("orders.ResultVersion", on_delete=models.PROTECT)
    operator = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    evidence_code = models.CharField(max_length=32)


class ResultAcknowledgment(ScopedRecord):
    """An accountable clinician disposition applies only to these exact bytes."""

    version = models.OneToOneField("orders.ResultVersion", on_delete=models.PROTECT)
    reviewer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    disposition = models.CharField(max_length=24)


class ResultRelease(ScopedRecord):
    """A release cannot imply delegated blanket access or delivery."""

    version = models.ForeignKey("orders.ResultVersion", on_delete=models.PROTECT)
    acknowledgment = models.ForeignKey(ResultAcknowledgment, on_delete=models.PROTECT)
    released_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    audience = models.CharField(max_length=8)
    delegate_grant = models.ForeignKey(
        "identity.PatientDelegateGrant", null=True, on_delete=models.PROTECT
    )

    class Meta:
        """One explicit release per version and audience binding."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("version", "audience", "delegate_grant"),
                nulls_distinct=False,
                name="orders_release_audience",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(audience="patient", delegate_grant__isnull=True)
                    | models.Q(audience="delegate", delegate_grant__isnull=False)
                ),
                name="orders_release_scope",
            ),
        ]


class ResultWorkItem(ScopedRecord):
    """DB-created provenance for an actual workflows.Task, not another queue."""

    version = models.ForeignKey("orders.ResultVersion", on_delete=models.PROTECT)
    task = models.OneToOneField("workflows.Task", on_delete=models.PROTECT)
    kind = models.CharField(max_length=16)

    class Meta:
        """Replayed events cannot multiply alerts or exception tasks."""

        constraints: ClassVar = [
            models.UniqueConstraint(fields=("version", "kind"), name="orders_work_once")
        ]
