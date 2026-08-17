"""Management TOTP preflight and locked verification."""

from __future__ import annotations

from dataclasses import dataclass

from django.db import connection, transaction
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.identity.management.base import LifecycleCommandError
from apps.identity.management.context import (
    LifecycleContext,
    assume_runtime_owner,
    scoped_owner_gucs,
)


@dataclass(frozen=True, slots=True)
class ManagementDevice:
    """Expose only one confirmed device's nonsecret selection fields."""

    pk: int
    persistent_id: str


def confirmed_device_preflight(
    context: LifecycleContext,
) -> tuple[ManagementDevice, ...]:
    """Enumerate confirmed operator devices in one read-only app transaction."""
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
        with scoped_owner_gucs(context), assume_runtime_owner(context):
            device_ids = tuple(
                TOTPDevice.objects.filter(
                    user_id=context.operator_id,
                    confirmed=True,
                )
                .order_by("pk")
                .values_list("pk", flat=True)
            )
    model_label = TOTPDevice.model_label()
    return tuple(
        ManagementDevice(pk=device_id, persistent_id=f"{model_label}/{device_id}")
        for device_id in device_ids
    )


def verify_management_totp(
    context: LifecycleContext,
    device: ManagementDevice,
    token: str,
) -> None:
    """Commit one locked current-token decision before domain work."""
    verification_failed = False
    with (
        transaction.atomic(),
        scoped_owner_gucs(context),
        assume_runtime_owner(context),
    ):
        loaded = (
            TOTPDevice.objects.select_for_update()
            .filter(
                pk=device.pk,
                user_id=context.operator_id,
                confirmed=True,
            )
            .first()
        )
        if loaded is None:
            verification_failed = True
        else:
            allowed, _details = loaded.verify_is_allowed()
            verification_failed = not allowed or not loaded.verify_token(token)
    if verification_failed:
        raise LifecycleCommandError
