# pyright: reportImplicitRelativeImport=false

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import psycopg
import pytest
from apps.identity.models import Clinic, Organization, User, UserClinicRole
from apps.scheduling.locks import identity_lock_keys
from django.contrib.auth.hashers import make_password
from django.db import connection, transaction
from django_otp.oath import TOTP

from database_urls import database_url_for_name
from management_command_support import (
    CommandResult,
    generated_password,
    run_management_command,
)
from otp_test_support import create_totp_device

if TYPE_CHECKING:
    from django_otp.plugins.otp_totp.models import TOTPDevice

pytestmark = pytest.mark.django_db(transaction=True)


@dataclass(frozen=True, slots=True)
class OwnerSetup:
    organization_id: UUID
    clinic_id: UUID
    owner_id: UUID
    device: TOTPDevice
    token: str


def _owner_setup(label: str) -> OwnerSetup:
    organization_id = uuid4()
    clinic_id = uuid4()
    owner_id = uuid4()
    owner = User.objects.create(
        id=owner_id,
        username=f"synthetic-owner-{label}-{uuid4().hex}",
        password=make_password(generated_password()),
    )
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(organization_id)],
        )
        organization = Organization.objects.create(
            id=organization_id,
            name=f"Synthetic Race Organization {label}",
            cnpj=f"{int(organization_id) % 10**14:014d}",
        )
        clinic = Clinic.objects.create(
            id=clinic_id,
            organization=organization,
            name=f"Synthetic Race Clinic {label}",
            crm_uf="SP",
            timezone="America/Sao_Paulo",
        )
        UserClinicRole.objects.create(
            user=owner,
            organization=organization,
            clinic=clinic,
            role=UserClinicRole.Role.OWNER,
        )
    device = create_totp_device(owner_id, confirmed=True)
    generator = TOTP(
        device.bin_key,
        device.step,
        device.t0,
        device.digits,
        device.drift,
    )
    generator.time = time.time()
    return OwnerSetup(
        organization_id=organization_id,
        clinic_id=clinic_id,
        owner_id=owner_id,
        device=device,
        token=f"{generator.token():0{device.digits}d}",
    )


def _arguments(setup: OwnerSetup, target_id: UUID, username: str) -> tuple[str, ...]:
    return (
        "provision_staff",
        "--operator-id",
        str(setup.owner_id),
        "--organization-id",
        str(setup.organization_id),
        "--clinic-id",
        str(setup.clinic_id),
        "--staff-user-id",
        str(target_id),
        "--username",
        username,
        "--email",
        "Shared.Identity@EXAMPLE.invalid",
        "--role",
        UserClinicRole.Role.RECEPTIONIST,
    )


def _device_consumed(database_url: str, setup: OwnerSetup) -> bool:
    with psycopg.connect(database_url) as observer:
        observer.execute("SET LOCAL ROLE clinic_app")
        observer.execute(
            "SELECT pg_catalog.set_config('app.current_user_id', %s, true)",
            [str(setup.owner_id)],
        )
        row = observer.execute(
            "SELECT last_t FROM clinic_app.otp_totp_totpdevice WHERE id = %s",
            [setup.device.pk],
        ).fetchone()
    return row is not None and row[0] >= 0


def test_cross_clinic_equal_canonical_email_waits_on_one_global_gate() -> None:
    first = _owner_setup("A")
    second = _owner_setup("B")
    first_target = uuid4()
    second_target = uuid4()
    first_password = generated_password()
    second_password = generated_password()
    database_url = database_url_for_name(
        os.environ["MIGRATION_DATABASE_URL"],
        str(connection.settings_dict["NAME"]),
    )
    results: list[CommandResult] = []

    def invoke(
        setup: OwnerSetup,
        target_id: UUID,
        username: str,
        password: str,
    ) -> None:
        results.append(
            run_management_command(
                _arguments(setup, target_id, username),
                database_url=database_url,
                hidden_responses=(
                    ("Authentication code: ", setup.token),
                    ("Password: ", password),
                ),
            )
        )

    email_gate = next(
        key
        for key in identity_lock_keys("synthetic-a", "Shared.Identity@EXAMPLE.invalid")
        if key.startswith("clinic-lock-v1:identity-email:")
    )
    with psycopg.connect(database_url) as gate_connection:
        gate_connection.execute(
            "SELECT pg_catalog.pg_advisory_xact_lock("
            "pg_catalog.hashtextextended(%s, 0))",
            [email_gate],
        )
        workers = (
            threading.Thread(
                target=invoke,
                args=(first, first_target, "Synthetic.Race.A", first_password),
            ),
            threading.Thread(
                target=invoke,
                args=(second, second_target, "Synthetic.Race.B", second_password),
            ),
        )
        for worker in workers:
            worker.start()
        deadline = time.monotonic() + 10
        while not (
            _device_consumed(database_url, first)
            and _device_consumed(database_url, second)
        ):
            if time.monotonic() >= deadline:
                message = "management token commits did not finish"
                raise AssertionError(message)
        assert results == []
        assert all(worker.is_alive() for worker in workers)
        gate_connection.commit()
    for worker in workers:
        worker.join(timeout=10)

    assert all(not worker.is_alive() for worker in workers)
    assert sorted(result.exit_code for result in results) == [0, 1]
    assert all(first.token not in result.output for result in results)
    assert all(second.token not in result.output for result in results)
    assert all(first_password not in result.output for result in results)
    assert all(second_password not in result.output for result in results)
    assert User.objects.filter(email="shared.identity@example.invalid").count() == 1
    assert User.objects.filter(pk__in=(first_target, second_target)).count() == 1
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM clinic_app.audit_event "
            "WHERE event_type = 'identity.staff.provisioned'"
        )
        assert cursor.fetchone() == (1,)
