from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Final

import pytest
from apps.identity.auth_backends import ClinicBackend
from apps.identity.models import User
from apps.tenancy.db import tenant_context
from apps.tenancy.management import TenantCommand
from django.contrib.auth.hashers import make_password
from django.core.management import CommandError, call_command
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

if TYPE_CHECKING:
    from collections.abc import Iterator

    from conftest import TenantGraph

pytestmark = pytest.mark.django_db(transaction=True)

RAW_CREDENTIAL: Final = "correct horse battery staple"


@contextmanager
def _runtime_role() -> Iterator[None]:
    with connection.cursor() as cursor:
        cursor.execute("SET ROLE clinic_app")
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute("RESET ROLE")


def _gucs() -> tuple[str | None, str | None]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT current_setting('app.current_user_id', true), "
            "current_setting('app.current_tenant', true)"
        )
        row = cursor.fetchone()
    assert row is not None
    user_value, tenant_value = row
    assert user_value is None or isinstance(user_value, str)
    assert tenant_value is None or isinstance(tenant_value, str)
    return user_value, tenant_value


def test_clinic_backend_login_uses_auth_lookup_without_user_writes(
    tenant_graph: TenantGraph,
) -> None:
    encoded = make_password(RAW_CREDENTIAL)
    User.objects.filter(pk=tenant_graph.user_a).update(password=encoded)
    client = Client()

    with _runtime_role(), CaptureQueriesContext(connection) as queries:
        authenticated = ClinicBackend().authenticate(
            None,
            username=tenant_graph.username_a,
            password=RAW_CREDENTIAL,
        )
        assert client.login(
            username=tenant_graph.username_a,
            password=RAW_CREDENTIAL,
        )

    assert authenticated is not None
    assert authenticated._state.adding
    statements = "\n".join(query["sql"] for query in queries.captured_queries)
    assert "clinic_app.auth_lookup" in statements
    assert 'UPDATE "identity_user"' not in statements
    assert 'FROM "identity_user"' not in statements
    stored = User.objects.get(pk=tenant_graph.user_a)
    assert stored.password == encoded
    assert stored.last_login is None


@pytest.mark.parametrize("case", ["wrong-password", "inactive"])
def test_clinic_backend_returns_none_for_invalid_credentials(
    tenant_graph: TenantGraph,
    case: str,
) -> None:
    encoded = make_password(RAW_CREDENTIAL)
    User.objects.filter(pk=tenant_graph.user_a).update(
        password=encoded,
        is_active=case != "inactive",
    )
    password = "wrong" if case == "wrong-password" else RAW_CREDENTIAL

    with _runtime_role():
        result = ClinicBackend().authenticate(
            None,
            username=tenant_graph.username_a,
            password=password,
        )

    assert result is None


def test_clinic_backend_get_user_is_guc_bound_and_reconstructs_from_db(
    tenant_graph: TenantGraph,
) -> None:
    with (
        _runtime_role(),
        tenant_context(tenant_graph.user_a, tenant_graph.organization_a),
        CaptureQueriesContext(connection) as queries,
    ):
        loaded = ClinicBackend().get_user(tenant_graph.user_a)
        arbitrary = ClinicBackend().get_user(tenant_graph.user_b)

    assert loaded is not None
    assert loaded.pk == tenant_graph.user_a
    assert not loaded._state.adding
    assert loaded._state.db == "default"
    assert arbitrary is None
    statements = "\n".join(query["sql"] for query in queries.captured_queries)
    assert statements.count("clinic_app.load_current_user()") == 2
    assert str(tenant_graph.user_b) not in statements


class ProbeTenantCommand(TenantCommand):
    observed: tuple[str | None, str | None] | None = None

    def handle_tenant(
        self,
        *args: str,
        **options: str | int | bool | None | list[str],
    ) -> str:
        self.observed = _gucs()
        return "handled"


@pytest.mark.parametrize(
    "options",
    [
        {},
        {"user": "malformed", "tenant": "malformed"},
        {"user": "00000000-0000-0000-0000-000000000000"},
    ],
)
def test_tenant_command_rejects_missing_or_invalid_context_before_handler(
    options: dict[str, str],
) -> None:
    command = ProbeTenantCommand()

    with pytest.raises(CommandError):
        call_command(command, **options)

    assert command.observed is None


def test_tenant_command_runs_handler_inside_valid_context(
    tenant_graph: TenantGraph,
) -> None:
    command = ProbeTenantCommand()

    with _runtime_role():
        call_command(
            command,
            user=str(tenant_graph.user_a),
            tenant=str(tenant_graph.organization_a),
        )
        assert command.observed == (
            str(tenant_graph.user_a),
            str(tenant_graph.organization_a),
        )
        assert all(value in (None, "") for value in _gucs())
