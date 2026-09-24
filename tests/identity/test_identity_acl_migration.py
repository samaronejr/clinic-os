from __future__ import annotations

from uuid import UUID

import pytest
from apps.identity.auth_backends import ClinicBackend
from apps.identity.identifiers import canonicalize_email, canonicalize_username
from apps.identity.models import User, UserClinicRole
from django.contrib.auth.hashers import make_password
from django.db import connection, transaction

from identity.identity_acl_migration_support import (
    FOUNDATION_TARGETS,
    LOWEST_ROLE,
    MIGRATION_NAME,
    ORG_A,
    ORG_B,
    SHARED_USER,
    SINGLE_USER,
    TEST_CREDENTIAL,
    ZERO_USER,
    migrate,
    migrate_head,
    migration_module,
    seed_foundation_graph,
    set_tenant,
)
from patients.test_intake_migrations import _default_connection, _scratch_database

pytestmark = pytest.mark.django_db(transaction=True)


def test_migration_graph_and_frozen_canonicalizers_are_exact() -> None:
    module = migration_module()
    migration = module.Migration
    assert migration.dependencies == [
        ("identity", "0003_totp_device_rls"),
        ("tenancy", "0002_rls_and_resolvers"),
    ]
    frozen_username = module.canonicalize_username_for_migration
    frozen_email = module.canonicalize_email_for_migration
    for value in (
        "  \N{FULLWIDTH LATIN CAPITAL LETTER A}lice  ",
        "  \N{KELVIN SIGN}ELVIN  ",
    ):
        assert frozen_username(value) == canonicalize_username(value)
    assert frozen_email("  Å@EXAMPLE.COM  ") == canonicalize_email("  Å@EXAMPLE.COM  ")


def test_foundation_upgrade_canonicalizes_all_users_and_keeps_lowest_role(
    superuser_database_url: str,
) -> None:
    # The protected-field migrations are irreversible, so the migrate-back
    # to the foundation targets runs on a scratch database that never
    # applied them.
    with (
        _scratch_database(superuser_database_url) as wrapper,
        _default_connection(wrapper),
    ):
        migrate(FOUNDATION_TARGETS)
        originals = seed_foundation_graph()
        with connection.cursor() as cursor:
            cursor.execute("SET app.current_user_id = %s", [str(SHARED_USER)])
            cursor.execute("SET app.current_tenant = %s", [str(ORG_B)])
            cursor.execute("SET ROLE clinic_owner")
        try:
            migrate([("identity", MIGRATION_NAME)])
            users = {
                row.id: (row.username, row.email, row.password)
                for row in User.objects.filter(id__in=originals).order_by("id")
            }
            assert users == {
                user_id: (
                    canonicalize_username(username),
                    canonicalize_email(email),
                    password,
                )
                for user_id, (username, email, password) in originals.items()
            }
            with transaction.atomic():
                set_tenant(ORG_A)
                retained = list(
                    UserClinicRole.objects.filter(
                        user_id=SINGLE_USER,
                        role=UserClinicRole.Role.OWNER,
                    ).values_list("id", flat=True)
                )
            assert retained == [LOWEST_ROLE]
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT current_setting('role'), "
                    "current_setting('app.current_user_id', true), "
                    "current_setting('app.current_tenant', true), "
                    "to_regprocedure(%s)",
                    ["clinic_app._phase1a_identity_canonicalization_candidates()"],
                )
                assert cursor.fetchone() == (
                    "clinic_owner",
                    str(SHARED_USER),
                    str(ORG_B),
                    None,
                )
                cursor.execute("SET ROLE clinic_app")
            original_login = ClinicBackend().authenticate(
                None,
                username=originals[ZERO_USER][0],
                password=TEST_CREDENTIAL,
            )
            canonical_login = ClinicBackend().authenticate(
                None,
                username=canonicalize_username(originals[ZERO_USER][0]),
                password=TEST_CREDENTIAL,
            )
            assert original_login is not None
            assert original_login.pk == ZERO_USER
            assert canonical_login is not None
            assert canonical_login.pk == ZERO_USER
        finally:
            with connection.cursor() as cursor:
                cursor.execute("RESET ROLE")
                cursor.execute("RESET app.current_user_id")
                cursor.execute("RESET app.current_tenant")
            migrate_head()


@pytest.mark.parametrize(
    ("users", "expected_message"),
    [
        (
            [
                (
                    UUID(int=401),
                    "\N{FULLWIDTH LATIN CAPITAL LETTER A}lice",
                    "one@example.com",
                ),
                ((UUID(int=402)), "Alice", "two@example.com"),
            ],
            "identity canonicalization preflight failed",
        ),
        (
            [
                ((UUID(int=403)), "first", "Å@example.com"),
                ((UUID(int=404)), "second", "Å@EXAMPLE.COM"),
            ],
            "identity canonicalization preflight failed",
        ),
        (
            [((UUID(int=405)), "   ", "valid@example.com")],
            "identity canonicalization preflight failed",
        ),
    ],
)
def test_canonicalization_failure_aborts_before_any_update(
    users: list[tuple[UUID, str, str]],
    expected_message: str,
    superuser_database_url: str,
) -> None:
    with (
        _scratch_database(superuser_database_url) as wrapper,
        _default_connection(wrapper),
    ):
        migrate(FOUNDATION_TARGETS)
        encoded = make_password(TEST_CREDENTIAL)
        for user_id, username, email in users:
            User.objects.create(
                id=user_id, username=username, email=email, password=encoded
            )
        originals = list(
            User.objects.filter(id__in=[row[0] for row in users])
            .order_by("id")
            .values_list("id", "username", "email")
        )
        try:
            with pytest.raises(RuntimeError, match=expected_message):
                migrate([("identity", MIGRATION_NAME)])
            assert (
                list(
                    User.objects.filter(id__in=[row[0] for row in users])
                    .order_by("id")
                    .values_list("id", "username", "email")
                )
                == originals
            )
        finally:
            with connection.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM clinic_app.identity_user WHERE id = ANY(%s)",
                    [[row[0] for row in users]],
                )
            migrate_head()


def test_identity_migration_reverses_and_reapplies(
    superuser_database_url: str,
) -> None:
    with (
        _scratch_database(superuser_database_url) as wrapper,
        _default_connection(wrapper),
    ):
        try:
            migrate(FOUNDATION_TARGETS)
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT to_regprocedure(%s)",
                    ["clinic_app.list_active_clinic_physicians(uuid)"],
                )
                assert cursor.fetchone() == (None,)
            migrate([("identity", MIGRATION_NAME)])
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT to_regprocedure(%s)",
                    ["clinic_app.list_active_clinic_physicians(uuid)"],
                )
                assert cursor.fetchone()[0] is not None
        finally:
            migrate_head()
