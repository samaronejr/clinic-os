# pyright: reportImplicitRelativeImport=false

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from apps.identity.models import Clinic, Organization, User, UserClinicRole
from apps.scheduling.models import AvailabilityBlock
from apps.scheduling.timezones import parse_local_minute
from django.contrib.auth.hashers import make_password
from django.db import connection, transaction
from django.utils import timezone
from django_otp.oath import TOTP
from django_otp.plugins.otp_totp.models import TOTPDevice

from database_urls import database_url_for_name
from infra.management_command_support import generated_password, run_management_command
from otp_test_support import (
    create_totp_device,
    current_user_guc,
    get_totp_device,
    runtime_role,
)

pytestmark = pytest.mark.django_db(transaction=True)


@dataclass(frozen=True, slots=True)
class OwnerSetup:
    organization_id: UUID
    clinic_id: UUID
    owner_id: UUID
    owner_role_id: UUID
    token: str
    token_counter: int


def _owner_setup() -> OwnerSetup:
    organization_id = uuid4()
    clinic_id = uuid4()
    owner_id = uuid4()
    owner = User.objects.create(
        id=owner_id,
        username=f"synthetic-owner-{uuid4().hex}",
        password=make_password(generated_password()),
    )
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(organization_id)],
        )
        organization = Organization.objects.create(
            id=organization_id,
            name="Synthetic Revocation Organization",
            cnpj="00000000000000",
        )
        clinic = Clinic.objects.create(
            id=clinic_id,
            organization=organization,
            name="Synthetic Revocation Clinic",
            crm_uf="SP",
            timezone="America/Sao_Paulo",
        )
        owner_role = UserClinicRole.objects.create(
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
        owner_role_id=owner_role.pk,
        token=f"{generator.token():0{device.digits}d}",
        token_counter=generator.t(),
    )


def test_revoke_staff_role_refuses_to_remove_the_final_active_owner() -> None:
    setup = _owner_setup()
    database_url = database_url_for_name(
        os.environ["MIGRATION_DATABASE_URL"],
        str(connection.settings_dict["NAME"]),
    )

    result = run_management_command(
        (
            "revoke_staff_role",
            "--operator-id",
            str(setup.owner_id),
            "--organization-id",
            str(setup.organization_id),
            "--clinic-id",
            str(setup.clinic_id),
            "--target-user-id",
            str(setup.owner_id),
            "--role",
            UserClinicRole.Role.OWNER,
        ),
        database_url=database_url,
        hidden_responses=(("Authentication code: ", setup.token),),
    )

    assert result.exit_code != 0
    assert setup.token not in result.output
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(setup.organization_id)],
        )
        assert UserClinicRole.objects.filter(pk=setup.owner_role_id).exists()
        cursor.execute(
            "SELECT count(*) FROM clinic_app.audit_event WHERE organization_id = %s",
            [setup.organization_id],
        )
        assert cursor.fetchone() == (0,)
    refreshed = get_totp_device(setup.owner_id, confirmed=True)
    assert refreshed.last_t >= setup.token_counter


def _current_token(device_id: int, owner_id: UUID) -> tuple[str, int, str]:
    with runtime_role(), current_user_guc(owner_id):
        device = TOTPDevice.objects.get(pk=device_id)
    generator = TOTP(
        device.bin_key,
        device.step,
        device.t0,
        device.digits,
        device.drift,
    )
    generator.time = time.time()
    return (
        f"{generator.token():0{device.digits}d}",
        generator.t(),
        device.persistent_id,
    )


def test_physician_revocation_blocks_active_work_and_preserves_retired_history() -> (
    None
):
    setup = _owner_setup()
    physician_id = uuid4()
    physician = User.objects.create(
        id=physician_id,
        username=f"synthetic-physician-{uuid4().hex}",
        password=make_password(generated_password()),
    )
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(setup.organization_id)],
        )
        physician_role = UserClinicRole.objects.create(
            user=physician,
            organization_id=setup.organization_id,
            clinic_id=setup.clinic_id,
            role=UserClinicRole.Role.PHYSICIAN,
        )
        local_day = (timezone.localdate() + timedelta(days=2)).isoformat()
        availability = AvailabilityBlock.objects.create(
            organization_id=setup.organization_id,
            clinic_id=setup.clinic_id,
            practitioner=physician,
            start_at=parse_local_minute(f"{local_day}T10:00", "America/Sao_Paulo"),
            end_at=parse_local_minute(f"{local_day}T11:00", "America/Sao_Paulo"),
            idempotency_key=uuid4(),
            create_fingerprint=b"p" * 32,
        )
    first_device = get_totp_device(setup.owner_id, confirmed=True)
    second_device = create_totp_device(setup.owner_id, confirmed=True)
    first_token, first_counter, first_persistent_id = _current_token(
        first_device.pk,
        setup.owner_id,
    )
    second_token, second_counter, second_persistent_id = _current_token(
        second_device.pk,
        setup.owner_id,
    )
    database_url = database_url_for_name(
        os.environ["MIGRATION_DATABASE_URL"],
        str(connection.settings_dict["NAME"]),
    )
    arguments = (
        "revoke_staff_role",
        "--operator-id",
        str(setup.owner_id),
        "--organization-id",
        str(setup.organization_id),
        "--clinic-id",
        str(setup.clinic_id),
        "--target-user-id",
        str(physician_id),
        "--role",
        UserClinicRole.Role.PHYSICIAN,
    )

    blocked = run_management_command(
        arguments,
        database_url=database_url,
        hidden_responses=(
            ("Authenticator ID: ", first_persistent_id),
            ("Authentication code: ", first_token),
        ),
    )

    assert blocked.exit_code != 0
    assert first_token not in blocked.output
    assert first_persistent_id not in blocked.output
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(setup.organization_id)],
        )
        assert UserClinicRole.objects.filter(pk=physician_role.pk).exists()
        availability.retired_at = timezone.now()
        availability.save(update_fields=("retired_at", "updated_at"))

    revoked = run_management_command(
        arguments,
        database_url=database_url,
        hidden_responses=(
            ("Authenticator ID: ", second_persistent_id),
            ("Authentication code: ", second_token),
        ),
    )

    assert revoked.exit_code == 0, revoked.output
    assert second_token not in revoked.output
    assert second_persistent_id not in revoked.output
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(setup.organization_id)],
        )
        assert not UserClinicRole.objects.filter(pk=physician_role.pk).exists()
        assert AvailabilityBlock.objects.filter(pk=availability.pk).exists()
        assert User.objects.filter(pk=physician_id).exists()
        cursor.execute(
            "SELECT event_type, affected_record_id, payload->>'object_verb' "
            "FROM clinic_app.audit_event WHERE organization_id = %s",
            [setup.organization_id],
        )
        assert cursor.fetchall() == [
            (
                "identity.staff_role.revoked",
                str(physician_role.pk),
                "revoked",
            )
        ]
    with runtime_role(), current_user_guc(setup.owner_id):
        stored_first = TOTPDevice.objects.get(pk=first_device.pk)
        stored_second = TOTPDevice.objects.get(pk=second_device.pk)
    assert stored_first.last_t >= first_counter
    assert stored_second.last_t >= second_counter
