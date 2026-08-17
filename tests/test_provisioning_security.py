# pyright: reportImplicitRelativeImport=false

from __future__ import annotations

import os
import time
from uuid import UUID, uuid4

import pytest
from apps.identity.models import Clinic, Organization, User, UserClinicRole
from apps.intake.models import Patient, PatientClinicEnrollment
from django.contrib.auth.hashers import make_password
from django.core.management import CommandError, call_command
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext
from django_otp.oath import TOTP

from database_urls import database_url_for_name
from management_command_support import generated_password, run_management_command
from otp_test_support import create_totp_device, get_totp_device

pytestmark = pytest.mark.django_db(transaction=True)


def _token_for(user_id: UUID) -> str:
    device = get_totp_device(user_id, confirmed=True)
    generator = TOTP(
        device.bin_key,
        device.step,
        device.t0,
        device.digits,
        device.drift,
    )
    generator.time = time.time()
    return f"{generator.token():0{device.digits}d}"


def test_timezone_change_refuses_after_the_first_clinic_enrollment() -> None:
    organization_id = uuid4()
    clinic_id = uuid4()
    owner_id = uuid4()
    owner = User.objects.create(
        id=owner_id,
        username=f"synthetic-security-owner-{uuid4().hex}",
        password=make_password(generated_password()),
    )
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(organization_id)],
        )
        organization = Organization.objects.create(
            id=organization_id,
            name="Synthetic Enrollment Organization",
            cnpj="00000000000000",
        )
        clinic = Clinic.objects.create(
            id=clinic_id,
            organization=organization,
            name="Synthetic Enrollment Clinic",
            crm_uf="SP",
            timezone="America/Sao_Paulo",
        )
        UserClinicRole.objects.create(
            user=owner,
            organization=organization,
            clinic=clinic,
            role=UserClinicRole.Role.OWNER,
        )
        patient = Patient.objects.create(
            organization=organization,
            full_name="Synthetic Enrollment Patient",
            birth_date="2000-01-02",
        )
        PatientClinicEnrollment.objects.create(
            organization=organization,
            clinic=clinic,
            patient=patient,
            idempotency_key=uuid4(),
            create_fingerprint=b"e" * 32,
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
    database_url = database_url_for_name(
        os.environ["MIGRATION_DATABASE_URL"],
        str(connection.settings_dict["NAME"]),
    )

    result = run_management_command(
        (
            "set_clinic_timezone",
            "--operator-id",
            str(owner_id),
            "--organization-id",
            str(organization_id),
            "--clinic-id",
            str(clinic_id),
            "--timezone",
            "America/Manaus",
        ),
        database_url=database_url,
        hidden_responses=(("Authentication code: ", token),),
    )

    assert result.exit_code != 0
    assert token not in result.output
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(organization_id)],
        )
        clinic.refresh_from_db()
        cursor.execute(
            "SELECT count(*) FROM clinic_app.audit_event WHERE organization_id = %s",
            [organization_id],
        )
        assert cursor.fetchone() == (0,)
    assert clinic.timezone == "America/Sao_Paulo"
    assert get_totp_device(owner_id, confirmed=True).last_t >= generator.t()


def test_only_owner_union_can_provision_staff() -> None:
    organization_id = uuid4()
    clinic_id = uuid4()
    owner_id = uuid4()
    admin_id = uuid4()
    owner = User.objects.create(
        id=owner_id,
        username=f"synthetic-union-owner-{uuid4().hex}",
        password=make_password(generated_password()),
    )
    admin = User.objects.create(
        id=admin_id,
        username=f"synthetic-union-admin-{uuid4().hex}",
        password=make_password(generated_password()),
    )
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(organization_id)],
        )
        organization = Organization.objects.create(
            id=organization_id,
            name="Synthetic Union Organization",
            cnpj="00000000000000",
        )
        clinic = Clinic.objects.create(
            id=clinic_id,
            organization=organization,
            name="Synthetic Union Clinic",
            crm_uf="SP",
            timezone="America/Sao_Paulo",
        )
        UserClinicRole.objects.bulk_create(
            (
                UserClinicRole(
                    user=owner,
                    organization=organization,
                    clinic=clinic,
                    role=UserClinicRole.Role.OWNER,
                ),
                UserClinicRole(
                    user=owner,
                    organization=organization,
                    clinic=clinic,
                    role=UserClinicRole.Role.PHYSICIAN,
                ),
                UserClinicRole(
                    user=admin,
                    organization=organization,
                    clinic=clinic,
                    role=UserClinicRole.Role.CLINIC_ADMIN,
                ),
            )
        )
    create_totp_device(owner_id, confirmed=True)
    create_totp_device(admin_id, confirmed=True)
    owner_token = _token_for(owner_id)
    admin_token = _token_for(admin_id)
    owner_password = generated_password()
    admin_password = generated_password()
    owner_target = uuid4()
    admin_target = uuid4()
    database_url = database_url_for_name(
        os.environ["MIGRATION_DATABASE_URL"],
        str(connection.settings_dict["NAME"]),
    )

    def arguments(
        operator_id: UUID,
        target_id: UUID,
        username: str,
    ) -> tuple[str, ...]:
        return (
            "provision_staff",
            "--operator-id",
            str(operator_id),
            "--organization-id",
            str(organization_id),
            "--clinic-id",
            str(clinic_id),
            "--staff-user-id",
            str(target_id),
            "--username",
            username,
            "--email",
            f"{username}@example.invalid",
            "--role",
            UserClinicRole.Role.RECEPTIONIST,
        )

    denied = run_management_command(
        arguments(admin_id, admin_target, "synthetic.admin.target"),
        database_url=database_url,
        hidden_responses=(
            ("Authentication code: ", admin_token),
            ("Password: ", admin_password),
        ),
    )
    allowed = run_management_command(
        arguments(owner_id, owner_target, "synthetic.owner.target"),
        database_url=database_url,
        hidden_responses=(
            ("Authentication code: ", owner_token),
            ("Password: ", owner_password),
        ),
    )

    assert denied.exit_code != 0
    assert "Authentication code:" not in denied.output
    assert allowed.exit_code == 0, allowed.output
    assert admin_token not in denied.output
    assert admin_password not in denied.output
    assert owner_token not in allowed.output
    assert owner_password not in allowed.output
    assert not User.objects.filter(pk=admin_target).exists()
    assert User.objects.filter(pk=owner_target).exists()
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM clinic_app.audit_event "
            "WHERE event_type = 'identity.staff.provisioned'"
        )
        assert cursor.fetchone() == (1,)


def test_non_tty_call_command_fails_before_any_database_query() -> None:
    with (
        CaptureQueriesContext(connection) as queries,
        pytest.raises(CommandError, match="owner lifecycle command failed"),
    ):
        call_command(
            "provision_staff",
            operator_id=str(uuid4()),
            organization_id=str(uuid4()),
            clinic_id=str(uuid4()),
            staff_user_id=str(uuid4()),
            username="synthetic.no.tty",
            email="synthetic.no.tty@example.invalid",
            role=UserClinicRole.Role.RECEPTIONIST,
        )

    assert queries.captured_queries == []
