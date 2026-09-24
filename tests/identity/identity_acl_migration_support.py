from __future__ import annotations

import importlib
import importlib.util
from typing import TYPE_CHECKING, Final
from uuid import UUID

from apps.identity.models import Organization, User, UserClinicRole
from django.contrib.auth.hashers import make_password
from django.db import connection, transaction
from django.db.migrations.executor import MigrationExecutor

if TYPE_CHECKING:
    from types import ModuleType

MIGRATION_NAME: Final = "0004_current_actor_acl_and_physician_catalog"
MIGRATION_MODULE: Final = f"apps.identity.migrations.{MIGRATION_NAME}"
FOUNDATION_TARGETS: Final = [
    ("identity", "0003_totp_device_rls"),
    ("tenancy", "0002_rls_and_resolvers"),
]
TEST_CREDENTIAL: Final = "synthetic-migration-credential"
ZERO_USER = UUID(int=101)
SINGLE_USER = UUID(int=102)
SHARED_USER = UUID(int=103)
ORG_A = UUID(int=201)
ORG_B = UUID(int=202)
CLINIC_A = UUID(int=301)
CLINIC_B = UUID(int=302)
LOWEST_ROLE = UUID(int=10)


def migration_module() -> ModuleType:
    assert importlib.util.find_spec(MIGRATION_MODULE) is not None
    return importlib.import_module(MIGRATION_MODULE)


def migrate(targets: list[tuple[str, str]]) -> None:
    MigrationExecutor(connection).migrate(targets)


def migrate_head() -> None:
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())


def set_tenant(organization_id: UUID) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(organization_id)],
        )


def seed_foundation_graph() -> dict[UUID, tuple[str, str, str]]:
    encoded = make_password(TEST_CREDENTIAL)
    originals = {
        ZERO_USER: (
            "  \N{FULLWIDTH LATIN CAPITAL LETTER A}lice.Zero  ",
            "  ZERO@EXAMPLE.COM  ",
            encoded,
        ),
        SINGLE_USER: (
            "  \N{KELVIN SIGN}ELVIN  ",
            "  SINGLE@EXAMPLE.COM  ",
            encoded,
        ),
        SHARED_USER: (
            "  \N{FULLWIDTH LATIN CAPITAL LETTER B}ob.Shared  ",
            "shared@example.com",
            encoded,
        ),
    }
    for user_id, (username, email, password) in originals.items():
        User.objects.create(
            id=user_id,
            username=username,
            email=email,
            password=password,
            is_active=True,
        )
    for organization_id, clinic_id, suffix in (
        (ORG_A, CLINIC_A, "A"),
        (ORG_B, CLINIC_B, "B"),
    ):
        with transaction.atomic():
            set_tenant(organization_id)
            organization = Organization.objects.create(
                id=organization_id,
                name=f"Synthetic Organization {suffix}",
                cnpj=f"{int(organization_id):014d}",
            )
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO clinic_app.identity_clinic "
                    "(id, organization_id, name, crm_uf) VALUES (%s, %s, %s, %s)",
                    [
                        clinic_id,
                        organization.pk,
                        f"Synthetic Clinic {suffix}",
                        "SP",
                    ],
                )
    with transaction.atomic():
        set_tenant(ORG_A)
        for role_id in (UUID(int=30), LOWEST_ROLE, UUID(int=20)):
            UserClinicRole.objects.create(
                id=role_id,
                user_id=SINGLE_USER,
                organization_id=ORG_A,
                clinic_id=CLINIC_A,
                role=UserClinicRole.Role.OWNER,
            )
        UserClinicRole.objects.create(
            user_id=SHARED_USER,
            organization_id=ORG_A,
            clinic_id=CLINIC_A,
            role=UserClinicRole.Role.RECEPTIONIST,
        )
    with transaction.atomic():
        set_tenant(ORG_B)
        UserClinicRole.objects.create(
            user_id=SHARED_USER,
            organization_id=ORG_B,
            clinic_id=CLINIC_B,
            role=UserClinicRole.Role.PHYSICIAN,
        )
    return originals
