"""Organization-scoped practitioner availability promises."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, ClassVar

from django.conf import settings
from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import (
    DateTimeRangeField,
    RangeBoundary,
    RangeOperators,
)
from django.db import models
from django.db.models import Q

from apps.identity.models import Clinic
from apps.tenancy.models import TenantScopedModel

if TYPE_CHECKING:
    from django.db.models.constraints import BaseConstraint


class AvailabilityBlock(TenantScopedModel):
    """One immutable UTC interval a practitioner promises to one clinic."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey(Clinic, on_delete=models.PROTECT)
    practitioner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
    )
    start_at = models.DateTimeField()
    end_at = models.DateTimeField()
    idempotency_key = models.UUIDField()
    create_fingerprint = models.BinaryField(max_length=32, editable=False)
    retired_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Keep active promises immediate and index manager query paths."""

        constraints: ClassVar[list[BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("organization", "idempotency_key"),
                name="scheduling_availability_org_idempotency_uniq",
            ),
            models.UniqueConstraint(
                fields=("organization", "clinic", "id"),
                name="scheduling_availability_org_clinic_id_uniq",
            ),
            ExclusionConstraint(
                name="scheduling_availability_active_practitioner_excl",
                expressions=(
                    ("practitioner", RangeOperators.EQUAL),
                    (
                        models.Func(
                            "start_at",
                            "end_at",
                            RangeBoundary(),
                            function="TSTZRANGE",
                            output_field=DateTimeRangeField(),
                        ),
                        RangeOperators.OVERLAPS,
                    ),
                ),
                condition=Q(retired_at__isnull=True),
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=("clinic", "start_at"),
                name="sched_avail_clinic_start_idx",
            ),
            models.Index(
                fields=("practitioner", "start_at"),
                name="sched_avail_pract_start_idx",
            ),
        ]
