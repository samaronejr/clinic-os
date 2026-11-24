"""The runtime sends only statement templates, including on idle backends."""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from typing import TYPE_CHECKING

import psycopg
import pytest
from apps.comms.inbox_inbound import inbound_context
from apps.tenancy import envelope
from apps.tenancy.db import tenant_context
from django.db import connection

from comms.support import inbox_account
from realtime.test_stream import runtime_connection

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from pytest_django.fixtures import SettingsWrapper

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)

SHAPES = (
    "SELECT $1::text",
    "SELECT pg_catalog.set_config('app.current_user_id', $1, true) "
    "WHERE current_user IN ('clinic_app', 'clinic_owner')",
    "SELECT pg_catalog.set_config('app.current_tenant', $1, true)",
    "SELECT clinic_app.tenant_encrypt($1, $2, $3)",
    "SELECT clinic_app.tenant_decrypt($1, $2, $3)",
    "SELECT clinic_app.protected_encrypt($1, $2, $3)",
    "SELECT clinic_app.protected_decrypt($1, $2, $3)",
    "SELECT * FROM clinic_app.comms_resolve_inbound($1, $2)",
    "SELECT set_config('app.current_tenant', $1, true), "
    "set_config('app.current_principal', $2, true), "
    "set_config('app.comms_inbound_proof', $3, true)",
)
BY_TEMPLATE = {re.sub(r"\$\d+", "%s", shape): shape for shape in SHAPES}


class _Observer:
    def __init__(
        self, reader: psycopg.Connection, pool: ThreadPoolExecutor, name: str
    ) -> None:
        self.reader = reader
        self.pool = pool
        self.name = name
        self.checked: list[str] = []

    def _matches(self, finished: Event, shape: str) -> bool:
        assert finished.wait(timeout=10)
        # Never fetch query text. Even on a regression, the observer returns
        # only a boolean for the uniquely tagged synthetic test connection.
        row = self.reader.execute(
            "SELECT query = %s FROM pg_catalog.pg_stat_activity "
            "WHERE application_name = %s AND datname = current_database() "
            "AND usename = session_user AND session_user = 'clinic_app' "
            "AND pid <> pg_backend_pid() AND state IN ('idle', 'idle in transaction')",
            [shape, self.name],
        ).fetchall()
        return row == [(True,)]

    def __call__(
        self,
        execute: Callable[..., object],
        sql: str,
        params: Sequence[object] | None,
        many: bool,
        context: dict[str, object],
    ) -> object:
        shape = BY_TEMPLATE.get(sql)
        if shape is None:
            return execute(sql, params, many, context)
        finished = Event()
        observed = self.pool.submit(self._matches, finished, shape)
        try:
            result = execute(sql, params, many, context)
        finally:
            finished.set()
        assert observed.result(), f"expected only this SQL template: {shape}"
        self.checked.append(shape)
        return result


def test_runtime_binds_every_envelope_and_webhook_argument(
    app_database_url: str, rbac_graph: RbacGraph, settings: SettingsWrapper
) -> None:
    inbox = inbox_account(rbac_graph, settings)
    with (
        runtime_connection() as name,
        psycopg.connect(app_database_url, autocommit=True) as reader,
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        observer = _Observer(reader, pool, name)
        with connection.execute_wrapper(observer):
            # Fail a binding mutation before sending any secret on this backend.
            with connection.cursor() as cursor:
                cursor.execute("SELECT %s::text", ["synthetic binding probe"])
                assert cursor.fetchone() == ("synthetic binding probe",)
            value = b"synthetic binding probe"
            purpose = "test.parameter_binding"
            with tenant_context(rbac_graph.physician, rbac_graph.organization_a):
                ciphertext = envelope.encrypt(purpose=purpose, plaintext=value)
                assert envelope.decrypt(purpose=purpose, envelope=ciphertext) == value
                ciphertext = envelope.protect(purpose=purpose, plaintext=value)
                assert envelope.reveal(purpose=purpose, envelope=ciphertext) == value
            with inbound_context(account_id=inbox.account, proof=inbox.key) as account:
                assert account.account_id == inbox.account
        assert tuple(observer.checked) == SHAPES


def test_runtime_cannot_enable_server_bind_detail(app_database_url: str) -> None:
    with psycopg.connect(app_database_url, autocommit=True) as runtime:
        assert runtime.execute("SELECT session_user, current_user").fetchone() == (
            "clinic_app",
            "clinic_app",
        )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            runtime.execute("SET log_parameter_max_length = -1")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            runtime.execute("SET log_error_verbosity = verbose")
        # PG16's on-error limit is USERSET, not SUSET. A role default or a
        # REVOKE cannot forbid this SET. The immutable server verbosity is
        # what excludes error context from the supported log destinations.
        with runtime.transaction(force_rollback=True):
            runtime.execute("SET LOCAL log_parameter_max_length_on_error = -1")
            row = runtime.execute(
                "SELECT current_setting('log_parameter_max_length'), "
                "current_setting('log_parameter_max_length_on_error'), "
                "current_setting('log_error_verbosity')"
            ).fetchone()
            assert row == ("0", "-1", "terse")
