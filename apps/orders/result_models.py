"""Immutable source versions; intake is not chart filing or clinical review."""

from __future__ import annotations

from typing import ClassVar

from django.conf import settings
from django.db import models

from apps.orders.order_models import ScopedRecord
from apps.tenancy.fields import EncryptedJSONField


class Result(ScopedRecord):
    """One source series; corrections append instead of replacing bytes."""


class ResultVersion(ScopedRecord):
    """Original bytes and submitted identity are permanently version-bound."""

    result = models.ForeignKey(
        Result, on_delete=models.PROTECT, related_name="versions"
    )
    number = models.PositiveIntegerField()
    predecessor = models.OneToOneField(
        "self", null=True, on_delete=models.PROTECT, related_name="successor"
    )
    event_id = models.UUIDField()
    fingerprint = models.CharField(max_length=64)
    proposed_order_id = models.UUIDField(null=True)
    identity = EncryptedJSONField(purpose="orders.result.identity")
    storage_key = models.CharField(max_length=64, unique=True)
    sha256 = models.CharField(max_length=64)
    size_bytes = models.PositiveIntegerField()
    content_type = models.CharField(max_length=32)
    critical = models.BooleanField(default=False)
    uploader = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT
    )
    lab_account = models.ForeignKey(
        "orders.LabAccount", null=True, on_delete=models.PROTECT
    )

    class Meta:
        """One source event, one numbered version, and one provenance identity."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("clinic", "event_id"), name="orders_result_event"
            ),
            models.UniqueConstraint(
                fields=("result", "number"), name="orders_result_number"
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(uploader__isnull=False, lab_account__isnull=True)
                    | models.Q(uploader__isnull=True, lab_account__isnull=False)
                ),
                name="orders_source_identity",
            ),
        ]


class ResultObservation(ScopedRecord):
    """Exact source text, including separator, unit, interval and abnormal flag."""

    version = models.ForeignKey(
        ResultVersion, on_delete=models.PROTECT, related_name="observations"
    )
    position = models.PositiveSmallIntegerField()
    source = EncryptedJSONField(purpose="orders.observation.source")

    class Meta:
        """Retain source ordering without merging observations or inferring flags."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("version", "position"), name="orders_observation_position"
            )
        ]
