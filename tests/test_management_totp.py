# pyright: reportImplicitRelativeImport=false

from __future__ import annotations

import os
import time
from uuid import uuid4

import pytest
from apps.audit.services import verify_chain
from apps.identity.management.base import LifecycleCommandError
from apps.identity.management.context import LifecycleContext
from apps.identity.management.totp import (
    confirmed_device_preflight,
    verify_management_totp,
)
from apps.identity.models import Clinic, Organization, User, UserClinicRole
from django.contrib.auth.hashers import make_password
from django.db import connection, transaction
from django_otp.oath import TOTP
from django_otp.plugins.otp_totp.models import TOTPDevice

from database_urls import database_url_for_name
from management_command_support import generated_password, run_management_command
from otp_test_support import (
    create_totp_device,
    current_user_guc,
    get_totp_device,
    runtime_role,
)

pytestmark = pytest.mark.django_db(transaction=True)


def test_provision_staff_uses_current_owner_totp_before_password_and_write() -> None:
    organization_id = uuid4()
    clinic_id = uuid4()
    owner_id = uuid4()
    staff_id = uuid4()
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
            name="Synthetic Management Organization",
            cnpj="00000000000000",
        )
        clinic = Clinic.objects.create(
            id=clinic_id,
            organization=organization,
            name="Synthetic Management Clinic",
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
    token = f"{generator.token():0{device.digits}d}"
    password = generated_password()
    database_url = database_url_for_name(
        os.environ["MIGRATION_DATABASE_URL"],
        str(connection.settings_dict["NAME"]),
    )

    result = run_management_command(
        (
            "provision_staff",
            "--operator-id",
            str(owner_id),
            "--organization-id",
            str(organization_id),
            "--clinic-id",
            str(clinic_id),
            "--staff-user-id",
            str(staff_id),
            "--username",
            "Synthetic.Staff",
            "--email",
            "Staff.Synthetic@EXAMPLE.invalid",
            "--role",
            UserClinicRole.Role.CLINIC_ADMIN,
        ),
        database_url=database_url,
        hidden_responses=(
            ("Authentication code: ", token),
            ("Password: ", password),
        ),
    )

    assert result.exit_code == 0, result.output
    assert result.output.index("Authentication code:") < result.output.index(
        "Password:"
    )
    assert "Authenticator ID:" not in result.output
    assert token not in result.output
    assert password not in result.output
    staff = User.objects.get(pk=staff_id)
    assert staff.username == "synthetic.staff"
    assert staff.email == "staff.synthetic@example.invalid"
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(organization_id)],
        )
        role = UserClinicRole.objects.get(user_id=staff_id)
        cursor.execute(
            "SELECT event_type, affected_record_type, affected_record_id, "
            "payload->>'clinic_id', payload->>'object_verb', "
            "(SELECT count(*) FROM jsonb_object_keys(payload)) "
            "FROM clinic_app.audit_event WHERE organization_id = %s",
            [organization_id],
        )
        audit_row = cursor.fetchone()
    assert role.role == UserClinicRole.Role.CLINIC_ADMIN
    assert audit_row == (
        "identity.staff.provisioned",
        "identity.user_clinic_role",
        str(role.pk),
        str(clinic_id),
        "provisioned",
        2,
    )
    refreshed_device = get_totp_device(owner_id, confirmed=True)
    assert refreshed_device.last_t >= generator.t()
    with runtime_role(), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true), "
            "pg_catalog.set_config('app.current_user_id', %s, true)",
            [str(organization_id), str(owner_id)],
        )
        verification = verify_chain(organization_id)
    assert verification.valid is True
    assert verification.row_count == 1


def test_failed_token_commits_throttle_and_restores_role_and_gucs() -> None:
    organization_id = uuid4()
    clinic_id = uuid4()
    owner_id = uuid4()
    owner = User.objects.create(
        id=owner_id,
        username=f"synthetic-throttle-owner-{uuid4().hex}",
        password=make_password(generated_password()),
    )
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(organization_id)],
        )
        organization = Organization.objects.create(
            id=organization_id,
            name="Synthetic Throttle Organization",
            cnpj="00000000000000",
        )
        clinic = Clinic.objects.create(
            id=clinic_id,
            organization=organization,
            name="Synthetic Throttle Clinic",
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
    valid_token = generator.token()
    wrong_token = f"{(valid_token + 1) % (10**device.digits):0{device.digits}d}"
    context = LifecycleContext(
        operator_id=owner_id,
        organization_id=organization_id,
        clinic_id=clinic_id,
    )
    saved_tenant = str(uuid4())
    saved_user = str(uuid4())
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET ROLE clinic_owner")
            cursor.execute(
                "SELECT pg_catalog.set_config('app.current_tenant', %s, false), "
                "pg_catalog.set_config('app.current_user_id', %s, false)",
                [saved_tenant, saved_user],
            )
            assert TOTPDevice.objects.filter(pk=device.pk).count() == 0
        devices = confirmed_device_preflight(context)
        assert len(devices) == 1
        with pytest.raises(LifecycleCommandError):
            verify_management_totp(context, devices[0], wrong_token)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT current_user, current_setting('role'), "
                "current_setting('app.current_tenant', true), "
                "current_setting('app.current_user_id', true)"
            )
            assert cursor.fetchone() == (
                "clinic_owner",
                "clinic_owner",
                saved_tenant,
                saved_user,
            )
        with runtime_role(), current_user_guc(owner_id):
            device.refresh_from_db()
        assert device.throttling_failure_count == 1
        assert device.last_t == -1
    finally:
        with connection.cursor() as cursor:
            cursor.execute("RESET ROLE")
            cursor.execute("RESET app.current_tenant")
            cursor.execute("RESET app.current_user_id")
