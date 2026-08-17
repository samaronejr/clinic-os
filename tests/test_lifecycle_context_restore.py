from uuid import uuid4

import pytest
from apps.identity.management.base import LifecycleCommandError
from apps.identity.management.context import scoped_lifecycle_gucs
from django.db import connection, transaction

pytestmark = pytest.mark.django_db(transaction=True)
CONTEXT_COLUMNS = 3


def _database_context() -> tuple[str, str, str]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT current_setting('role'), "
            "current_setting('app.current_tenant', true), "
            "current_setting('app.current_user_id', true)"
        )
        row = cursor.fetchone()
    if (
        row is None
        or len(row) != CONTEXT_COLUMNS
        or not isinstance(row[0], str)
        or not isinstance(row[1], str)
        or not isinstance(row[2], str)
    ):
        raise AssertionError
    return row[0], row[1], row[2]


def _raise_if_requested(raises: bool) -> None:
    if raises:
        raise RuntimeError


@pytest.mark.parametrize("raises", [False, True])
def test_lifecycle_context_restores_exact_role_and_gucs(raises: bool) -> None:
    saved_tenant = str(uuid4())
    saved_user = str(uuid4())
    organization_id = uuid4()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET ROLE clinic_owner")
            cursor.execute(
                "SELECT pg_catalog.set_config('app.current_tenant', %s, false), "
                "pg_catalog.set_config('app.current_user_id', %s, false)",
                [saved_tenant, saved_user],
            )
        try:
            with transaction.atomic(), scoped_lifecycle_gucs(organization_id, None):
                assert _database_context() == (
                    "clinic_owner",
                    str(organization_id),
                    "",
                )
                _raise_if_requested(raises)
        except RuntimeError:
            if not raises:
                raise
        assert _database_context() == ("clinic_owner", saved_tenant, saved_user)
    finally:
        with connection.cursor() as cursor:
            cursor.execute("RESET ROLE")
            cursor.execute("RESET app.current_tenant")
            cursor.execute("RESET app.current_user_id")


def test_lifecycle_context_rejects_unsupported_role_without_state_change() -> None:
    saved_tenant = str(uuid4())
    saved_user = str(uuid4())
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET ROLE clinic_app")
            cursor.execute(
                "SELECT pg_catalog.set_config('app.current_tenant', %s, false), "
                "pg_catalog.set_config('app.current_user_id', %s, false)",
                [saved_tenant, saved_user],
            )
        with (
            transaction.atomic(),
            pytest.raises(LifecycleCommandError),
            scoped_lifecycle_gucs(uuid4(), None),
        ):
            pass
        assert _database_context() == ("clinic_app", saved_tenant, saved_user)
    finally:
        with connection.cursor() as cursor:
            cursor.execute("RESET ROLE")
            cursor.execute("RESET app.current_tenant")
            cursor.execute("RESET app.current_user_id")


def test_lifecycle_context_requires_an_outer_transaction() -> None:
    with pytest.raises(LifecycleCommandError), scoped_lifecycle_gucs(uuid4(), None):
        pass
