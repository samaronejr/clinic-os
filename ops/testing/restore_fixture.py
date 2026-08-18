"""Seed the smallest exact synthetic source through Phase 1A domain code."""

from __future__ import annotations

import argparse
import os
from importlib import import_module
from typing import Final, Never, Protocol
from uuid import UUID

import django
from django.db import connection, transaction

ORGANIZATION_ID = UUID("10000000-0000-4000-8000-000000000020")
CLINIC_ID = UUID("20000000-0000-4000-8000-000000000020")
OWNER_ID = UUID("30000000-0000-4000-8000-000000000020")
MIN_PRIVATE_FD: Final = 3
MAX_PASSWORD_BYTES: Final = 1024


class _BootstrapFactory(Protocol):
    def __call__(
        self,
        *args: object,
        **kwargs: object,
    ) -> object: ...


class _BootstrapAction(Protocol):
    def __call__(self, request: object, password: str) -> None: ...


class _DeviceManager(Protocol):
    def create(self, **values: object) -> object: ...


class _DeviceModel(Protocol):
    objects: _DeviceManager


def bootstrap(password: str) -> None:
    """Create one synthetic organization, clinic, owner role, and system audit."""
    module = import_module("apps.identity.management.bootstrap")
    factory: _BootstrapFactory = module.BootstrapRequest
    action: _BootstrapAction = module.bootstrap_clinic
    request = factory(
        organization_id=ORGANIZATION_ID,
        organization_name="Synthetic Phase 1A Recovery Clinic",
        cnpj="00000000000000",
        clinic_id=CLINIC_ID,
        clinic_name="Synthetic Recovery Clinic",
        crm_uf="SP",
        timezone="America/Sao_Paulo",
        owner_user_id=OWNER_ID,
        owner_username="synthetic.recovery.owner",
        owner_email="recovery-owner@example.invalid",
    )
    action(request, password)


def create_totp() -> None:
    """Create one confirmed source-only TOTP row through its app-role RLS policy."""
    module = import_module("django_otp.plugins.otp_totp.models")
    device: _DeviceModel = module.TOTPDevice
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL app.current_user_id = %s", [str(OWNER_ID)])
        device.objects.create(
            user_id=OWNER_ID,
            name="synthetic-recovery-device",
            confirmed=True,
        )


def main() -> int:
    """Dispatch only the bootstrap or app-role TOTP fixture step."""
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("operation", choices=("bootstrap", "totp"))
    parser.add_argument("--password-fd", type=int)
    arguments = parser.parse_args()
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.test")
    django.setup()
    if arguments.operation == "totp":
        if arguments.password_fd is not None:
            _fail("TOTP fixture received an unexpected password descriptor")
        create_totp()
        return 0
    if arguments.password_fd is None or arguments.password_fd < MIN_PRIVATE_FD:
        _fail("bootstrap fixture requires a private password descriptor")
    password = bytearray(os.read(arguments.password_fd, MAX_PASSWORD_BYTES + 1))
    try:
        if (
            not password
            or len(password) > MAX_PASSWORD_BYTES
            or not password.endswith(b"\n")
        ):
            _fail("bootstrap password frame rejected")
        bootstrap(password[:-1].decode("utf-8"))
    finally:
        for index in range(len(password)):
            password[index] = 0
    return 0


def _fail(reason: str) -> Never:
    raise RuntimeError(reason)


if __name__ == "__main__":
    raise SystemExit(main())
