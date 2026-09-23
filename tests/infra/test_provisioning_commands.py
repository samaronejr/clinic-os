# pyright: reportImplicitRelativeImport=false

from __future__ import annotations

import os
import time
from uuid import uuid4

import pytest
from apps.audit.models import SYSTEM_ORG_ID
from apps.audit.services import verify_chain
from apps.identity.management.base import LifecycleCommandError
from apps.identity.management.context import LifecycleContext
from apps.identity.management.totp import (
    confirmed_device_preflight,
    verify_management_totp,
)
from apps.identity.models import Clinic, Organization, User, UserClinicRole
from django.contrib.auth import authenticate
from django.contrib.auth.hashers import make_password
from django.db import connection, transaction
from django_otp.oath import TOTP
from django_otp.plugins.otp_totp.models import TOTPDevice

from database_urls import database_url_for_name
from infra.management_command_support import generated_password, run_management_command
from otp_test_support import (
    create_totp_device,
    current_user_guc,
    runtime_role,
)

pytestmark = pytest.mark.django_db(transaction=True)


def test_bootstrap_is_actorless_totp_exempt_and_uses_the_system_chain() -> None:
    organization_id = uuid4()
    clinic_id = uuid4()
    owner_id = uuid4()
    password = generated_password()
    database_url = database_url_for_name(
        os.environ["MIGRATION_DATABASE_URL"],
        str(connection.settings_dict["NAME"]),
    )

    result = run_management_command(
        (
            "bootstrap_clinic",
            "--organization-id",
            str(organization_id),
            "--organization-name",
            "Synthetic Manaus Organization",
            "--cnpj",
            "00000000000000",
            "--clinic-id",
            str(clinic_id),
            "--clinic-name",
            "Synthetic Manaus Clinic",
            "--crm-uf",
            "SP",
            "--timezone",
            "America/Manaus",
            "--owner-user-id",
            str(owner_id),
            "--owner-username",
            "Synthetic.Owner",
            "--owner-email",
            "Owner.Synthetic@EXAMPLE.invalid",
        ),
        database_url=database_url,
        hidden_responses=(("Password: ", password),),
    )

    assert result.exit_code == 0, result.output
    assert "clinic bootstrapped" in result.output
    assert "Authentication code:" not in result.output
    assert password not in result.output
    owner = User.objects.get(pk=owner_id)
    assert owner.username == "synthetic.owner"
    assert owner.email == "owner.synthetic@example.invalid"
    with runtime_role():
        assert authenticate(username="Synthetic.Owner", password=password) is not None
        assert authenticate(username="synthetic.owner", password=password) is not None
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(organization_id)],
        )
        organization = Organization.objects.get(pk=organization_id)
        clinic = Clinic.objects.get(pk=clinic_id)
        role = UserClinicRole.objects.get(user_id=owner_id)
    assert organization.name == "Synthetic Manaus Organization"
    assert clinic.timezone == "America/Manaus"
    assert clinic.crm_uf == "SP"
    assert role.role == UserClinicRole.Role.OWNER
    assert TOTPDevice.objects.count() == 0
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT organization_id, actor_user_id, event_type, "
            "affected_record_type, affected_record_id, "
            "payload->>'clinic_id', payload->>'object_verb', "
            "(SELECT count(*) FROM jsonb_object_keys(payload)) "
            "FROM clinic_app.audit_event WHERE organization_id = %s",
            [SYSTEM_ORG_ID],
        )
        audit_row = cursor.fetchone()
    assert audit_row == (
        SYSTEM_ORG_ID,
        None,
        "ops.clinic.bootstrapped",
        "identity.organization",
        str(organization_id),
        str(clinic_id),
        "bootstrapped",
        2,
    )
    verification = verify_chain()
    assert verification.valid is True
    assert verification.row_count == 1


def test_set_clinic_timezone_changes_an_empty_clinic_with_tenant_audit() -> None:
    organization_id = uuid4()
    clinic_id = uuid4()
    owner_id = uuid4()
    owner = User.objects.create(
        id=owner_id,
        username=f"synthetic-timezone-owner-{uuid4().hex}",
        password=make_password(generated_password()),
    )
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(organization_id)],
        )
        organization = Organization.objects.create(
            id=organization_id,
            name="Synthetic Timezone Organization",
            cnpj="00000000000000",
        )
        clinic = Clinic.objects.create(
            id=clinic_id,
            organization=organization,
            name="Synthetic Timezone Clinic",
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

    assert result.exit_code == 0, result.output
    assert token not in result.output
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(organization_id)],
        )
        clinic.refresh_from_db()
        cursor.execute(
            "SELECT event_type, affected_record_type, affected_record_id, "
            "payload->>'clinic_id', payload->>'object_verb' "
            "FROM clinic_app.audit_event WHERE organization_id = %s",
            [organization_id],
        )
        audit_row = cursor.fetchone()
    assert clinic.timezone == "America/Manaus"
    assert audit_row == (
        "identity.clinic_timezone.changed",
        "identity.clinic",
        str(clinic_id),
        str(clinic_id),
        "timezone_changed",
    )


def test_management_token_replay_is_rejected_after_successful_commit() -> None:
    organization_id = uuid4()
    clinic_id = uuid4()
    owner_id = uuid4()
    owner = User.objects.create(
        id=owner_id,
        username=f"synthetic-replay-owner-{uuid4().hex}",
        password=make_password(generated_password()),
    )
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(organization_id)],
        )
        organization = Organization.objects.create(
            id=organization_id,
            name="Synthetic Replay Organization",
            cnpj="00000000000000",
        )
        clinic = Clinic.objects.create(
            id=clinic_id,
            organization=organization,
            name="Synthetic Replay Clinic",
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
    context = LifecycleContext(owner_id, organization_id, clinic_id)
    selected = confirmed_device_preflight(context)

    verify_management_totp(context, selected[0], token)
    with pytest.raises(LifecycleCommandError):
        verify_management_totp(context, selected[0], token)

    with runtime_role(), current_user_guc(owner_id):
        device.refresh_from_db()
    assert device.last_t >= generator.t()
    assert device.throttling_failure_count == 1
