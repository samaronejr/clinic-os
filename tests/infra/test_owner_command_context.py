# pyright: reportImplicitRelativeImport=false

from __future__ import annotations

import os
import threading
import time
from uuid import uuid4

import psycopg
import pytest
from apps.identity.models import Clinic, Organization, User, UserClinicRole
from apps.scheduling.locks import clinic_lock_key
from django.contrib.auth.hashers import make_password
from django.db import connection, transaction
from django_otp.oath import TOTP

from database_urls import database_url_for_name
from infra.management_command_support import (
    CommandResult,
    generated_password,
    run_management_command,
    wait_for_totp_commit,
)
from otp_test_support import create_totp_device, get_totp_device

pytestmark = pytest.mark.django_db(transaction=True)


def test_valid_token_commits_before_a_later_domain_failure() -> None:
    organization_id = uuid4()
    clinic_id = uuid4()
    owner_id = uuid4()
    existing_id = uuid4()
    owner = User.objects.create(
        id=owner_id,
        username=f"synthetic-owner-{uuid4().hex}",
        password=make_password(generated_password()),
    )
    User.objects.create(
        id=existing_id,
        username="synthetic.duplicate",
        email="existing.synthetic@example.invalid",
        password=make_password(generated_password()),
    )
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(organization_id)],
        )
        organization = Organization.objects.create(
            id=organization_id,
            name="Synthetic Failure Organization",
            cnpj="00000000000000",
        )
        clinic = Clinic.objects.create(
            id=clinic_id,
            organization=organization,
            name="Synthetic Failure Clinic",
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
    target_id = uuid4()
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
            "Synthetic.Duplicate",
            "--email",
            "new.synthetic@example.invalid",
            "--role",
            UserClinicRole.Role.RECEPTIONIST,
        ),
        database_url=database_url,
        hidden_responses=(
            ("Authentication code: ", token),
            ("Password: ", password),
        ),
    )

    assert result.exit_code != 0
    assert token not in result.output
    assert password not in result.output
    assert not User.objects.filter(pk=target_id).exists()
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM clinic_app.audit_event WHERE organization_id = %s",
            [organization_id],
        )
        assert cursor.fetchone() == (0,)
    refreshed = get_totp_device(owner_id, confirmed=True)
    assert refreshed.last_t >= generator.t()


def test_post_lock_reauthorization_rejects_an_owner_revoked_after_totp() -> None:
    organization_id = uuid4()
    clinic_id = uuid4()
    operator_id = uuid4()
    remaining_owner_id = uuid4()
    operator = User.objects.create(
        id=operator_id,
        username=f"synthetic-operator-{uuid4().hex}",
        password=make_password(generated_password()),
    )
    remaining_owner = User.objects.create(
        id=remaining_owner_id,
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
            name="Synthetic Reauthorization Organization",
            cnpj="00000000000000",
        )
        clinic = Clinic.objects.create(
            id=clinic_id,
            organization=organization,
            name="Synthetic Reauthorization Clinic",
            crm_uf="SP",
            timezone="America/Sao_Paulo",
        )
        operator_role = UserClinicRole.objects.create(
            user=operator,
            organization=organization,
            clinic=clinic,
            role=UserClinicRole.Role.OWNER,
        )
        UserClinicRole.objects.create(
            user=remaining_owner,
            organization=organization,
            clinic=clinic,
            role=UserClinicRole.Role.OWNER,
        )
    device = create_totp_device(operator_id, confirmed=True)
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
    target_id = uuid4()
    database_url = database_url_for_name(
        os.environ["MIGRATION_DATABASE_URL"],
        str(connection.settings_dict["NAME"]),
    )
    arguments = (
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
        "synthetic.postlock",
        "--email",
        "postlock.synthetic@example.invalid",
        "--role",
        UserClinicRole.Role.RECEPTIONIST,
    )
    results: list[CommandResult] = []

    def invoke() -> None:
        results.append(
            run_management_command(
                arguments,
                database_url=database_url,
                hidden_responses=(
                    ("Authentication code: ", token),
                    ("Password: ", password),
                ),
            )
        )

    with psycopg.connect(database_url) as lock_connection:
        lock_connection.execute(
            "SELECT pg_catalog.pg_advisory_xact_lock("
            "pg_catalog.hashtextextended(%s, 0))",
            [clinic_lock_key(clinic_id)],
        )
        worker = threading.Thread(target=invoke)
        worker.start()
        wait_for_totp_commit(database_url, operator_id, device.pk, generator.t())
        with psycopg.connect(database_url) as revoker:
            revoker.execute(
                "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
                [str(organization_id)],
            )
            revoker.execute(
                "DELETE FROM clinic_app.identity_userclinicrole WHERE id = %s",
                [operator_role.pk],
            )
        lock_connection.commit()
    worker.join(timeout=10)

    assert not worker.is_alive()
    assert len(results) == 1
    assert results[0].exit_code != 0
    assert token not in results[0].output
    assert password not in results[0].output
    assert not User.objects.filter(pk=target_id).exists()
    refreshed = get_totp_device(operator_id, confirmed=True)
    assert refreshed.last_t >= generator.t()
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM clinic_app.audit_event WHERE organization_id = %s",
            [organization_id],
        )
        assert cursor.fetchone() == (0,)
