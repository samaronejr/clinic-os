# pyright: reportImplicitRelativeImport=false

from __future__ import annotations

import os
import time
from uuid import uuid4

import pytest
from apps.identity.models import Clinic, Organization, User, UserClinicRole
from django.contrib.auth.hashers import make_password
from django.db import connection, transaction
from django_otp.oath import TOTP

from database_urls import database_url_for_name
from management_command_support import generated_password, run_management_command
from otp_test_support import create_totp_device, get_totp_device

pytestmark = pytest.mark.django_db(transaction=True)


def test_numeric_staff_password_is_rejected_before_identity_or_audit_write() -> None:
    organization_id = uuid4()
    clinic_id = uuid4()
    owner_id = uuid4()
    target_id = uuid4()
    owner = User.objects.create(
        id=owner_id,
        username=f"synthetic-password-owner-{uuid4().hex}",
        password=make_password(generated_password()),
    )
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(organization_id)],
        )
        organization = Organization.objects.create(
            id=organization_id,
            name="Synthetic Password Organization",
            cnpj="00000000000000",
        )
        clinic = Clinic.objects.create(
            id=clinic_id,
            organization=organization,
            name="Synthetic Password Clinic",
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
    numeric_password = str(uuid4().int)
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
            str(target_id),
            "--username",
            "synthetic.numeric.password",
            "--email",
            "numeric.password.synthetic@example.invalid",
            "--role",
            UserClinicRole.Role.RECEPTIONIST,
        ),
        database_url=database_url,
        hidden_responses=(
            ("Authentication code: ", token),
            ("Password: ", numeric_password),
        ),
    )

    assert result.exit_code != 0
    assert token not in result.output
    assert numeric_password not in result.output
    assert not User.objects.filter(pk=target_id).exists()
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM clinic_app.audit_event WHERE organization_id = %s",
            [organization_id],
        )
        assert cursor.fetchone() == (0,)
    assert get_totp_device(owner_id, confirmed=True).last_t >= generator.t()
