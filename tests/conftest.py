from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Final
from uuid import UUID, uuid4

import psycopg
import pytest
from apps.identity.models import Clinic, Organization, User, UserClinicRole
from apps.tenancy.models import TenantProbe
from django.db import connection, transaction

from database_urls import database_url_for_name
from rbac_fixtures import RbacGraph, rbac_graph
from tenant_key_support import issue_tenant_key_for, synthetic_secret_backend
from tenant_probe_support import tenant_probe_pair
from workspace_refusal_observer import RefusalObserver
from workspace_refusal_support import GUARD_STATS

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Iterator
    from pathlib import Path

    from _pytest.terminal import TerminalReporter

SYNTHETIC_AUTH_VALUE_A = "synthetic-hash-a"
SYNTHETIC_AUTH_VALUE_B = "synthetic-hash-b"
AUDIT_ROW_TRIGGER = "audit_event_immutable_row"
AUDIT_TRUNCATE_TRIGGER = "audit_event_immutable_truncate"
__all__: Final = (
    "RbacGraph",
    "rbac_graph",
    "synthetic_secret_backend",
    "tenant_probe_pair",
)


REFUSAL_OBSERVER: pytest.StashKey[RefusalObserver] = pytest.StashKey()
REFUSAL_INTEGRITY: pytest.StashKey[Callable[[], list[str]]] = pytest.StashKey()
REFUSAL_SESSION_ERRORS: pytest.StashKey[Callable[[], list[str]]] = pytest.StashKey()
REFUSAL_BEFORE: pytest.StashKey[int] = pytest.StashKey()
TAMPERING_BEFORE: pytest.StashKey[int] = pytest.StashKey()
REFUSAL_ISSUES: pytest.StashKey[list[str]] = pytest.StashKey()
REFUSAL_FAILED: pytest.StashKey[bool] = pytest.StashKey()


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Include the session-lock mirror whenever a test uses the runtime database.

    Todo 8 splits session locks from transaction-pooled queries; both aliases
    address the same test database. Django must permit and close both, rather
    than bypassing its connection guard or leaking session locks across tests.
    Explicit non-default database selections remain unchanged. The refusal
    exit oracle runs last, after every scenario it aggregates.
    """
    for item in items:
        marker = item.get_closest_marker("django_db")
        if marker is None:
            continue
        aliases = marker.kwargs.get("databases")
        if aliases == "__all__":
            continue
        selected = set(aliases or ("default",))
        if "default" in selected:
            options = dict(marker.kwargs)
            options["databases"] = sorted(selected | {"locks"})
            item.add_marker(
                pytest.mark.django_db(*marker.args, **options), append=False
            )
    # Preserve every existing scenario's order; only the aggregate oracle is last.
    items.sort(
        key=lambda item: item.name == "test_all_workspace_refusal_exits_are_executed"
    )


def pytest_sessionstart(session: pytest.Session) -> None:
    observer = RefusalObserver()
    session.config.stash[REFUSAL_OBSERVER] = observer
    session.config.stash[GUARD_STATS] = observer.stats
    observer.start()
    # Bound before tests run: later class patches cannot replace the checks.
    session.config.stash[REFUSAL_INTEGRITY] = observer.integrity_errors
    session.config.stash[REFUSAL_SESSION_ERRORS] = observer.session_errors


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> None:
    observer = item.config.stash[REFUSAL_OBSERVER]
    item.stash[REFUSAL_BEFORE] = observer.stats.violations
    item.stash[TAMPERING_BEFORE] = len(observer.tampering)
    item.stash[REFUSAL_ISSUES] = []
    item.stash[REFUSAL_FAILED] = False


def _check_observer(item: pytest.Item) -> None:
    observer = item.config.stash[REFUSAL_OBSERVER]
    errors = item.config.stash[REFUSAL_INTEGRITY]()
    item.stash[REFUSAL_ISSUES].extend(errors)
    observer.tampering.extend(errors)


@pytest.fixture(autouse=True)
def workspace_refusal_guard(request: pytest.FixtureRequest) -> Iterator[None]:
    """Fixture cleanup cannot silently remove the response observer."""
    yield
    _check_observer(request.node)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    report = yield
    if call.when == "call":
        _check_observer(item)
    observer = item.config.stash[REFUSAL_OBSERVER]
    issues = [
        *item.stash[REFUSAL_ISSUES],
        *observer.tampering[item.stash[TAMPERING_BEFORE] :],
    ]
    if observer.stats.violations > item.stash[REFUSAL_BEFORE]:
        issues.append("The test produced a session-writing refusal")
    if issues and report.passed and not item.stash[REFUSAL_FAILED]:
        report.outcome = "failed"
        report.longrepr = "\n".join(sorted(set(issues)))
    if report.failed:
        item.stash[REFUSAL_FAILED] = True
    return report


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    observer = session.config.stash[REFUSAL_OBSERVER]
    errors = session.config.stash[REFUSAL_SESSION_ERRORS]()
    observer.tampering.extend(errors)
    if errors:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
    observer.stop()
    observer.stats.trace_ns = observer.trace.elapsed_ns


def pytest_terminal_summary(
    terminalreporter: TerminalReporter, config: pytest.Config
) -> None:
    if GUARD_STATS in config.stash:
        terminalreporter.write_line(
            "REFUSAL_GUARD "
            + json.dumps(asdict(config.stash[GUARD_STATS]), sort_keys=True)
        )
        for error in sorted(set(config.stash[REFUSAL_OBSERVER].tampering)):
            terminalreporter.write_line(f"REFUSAL_GUARD_FAILURE {error}")


@dataclass(frozen=True, slots=True)
class TenantGraph:
    organization_a: UUID
    organization_b: UUID
    user_a: UUID
    user_b: UUID
    username_a: str


def _create_tenant_rows(
    organization_id: UUID,
    user: User,
    *,
    label: str,
) -> None:
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(organization_id)],
        )
        organization = Organization.objects.create(
            id=organization_id,
            name=f"Synthetic Organization {label}",
            cnpj=f"{int(organization_id) % 10**14:014d}",
        )
        clinic = Clinic.objects.create(
            organization=organization,
            name=f"Synthetic Clinic {label}",
            crm_uf="SP",
            timezone="America/Sao_Paulo",
        )
        UserClinicRole.objects.create(
            user=user,
            organization=organization,
            clinic=clinic,
            role=UserClinicRole.Role.OWNER,
        )
        TenantProbe.objects.create(
            organization=organization,
            label=f"probe-{label.lower()}",
        )


@pytest.fixture
def tenant_graph(synthetic_secret_backend: Path) -> TenantGraph:
    organization_a = uuid4()
    organization_b = uuid4()
    username_a = f"synthetic-user-a-{uuid4().hex}"
    user_a = User.objects.create(username=username_a, password=SYNTHETIC_AUTH_VALUE_A)
    user_b = User.objects.create(
        username=f"synthetic-user-b-{uuid4().hex}",
        password=SYNTHETIC_AUTH_VALUE_B,
    )
    _create_tenant_rows(organization_a, user_a, label="A")
    _create_tenant_rows(organization_b, user_b, label="B")
    issue_tenant_key_for(organization_a)
    issue_tenant_key_for(organization_b)
    return TenantGraph(
        organization_a=organization_a,
        organization_b=organization_b,
        user_a=user_a.pk,
        user_b=user_b.pk,
        username_a=username_a,
    )


@pytest.fixture
def app_database_url() -> str:
    database_name = str(connection.settings_dict["NAME"])
    return database_url_for_name(os.environ["APP_DATABASE_URL"], database_name)


@pytest.fixture
def superuser_database_url() -> str:
    database_name = str(connection.settings_dict["NAME"])
    return database_url_for_name(
        os.environ["TEST_SUPERUSER_DATABASE_URL"], database_name
    )


def _test_superuser_database_url() -> str:
    database_name = str(connection.settings_dict["NAME"])
    return database_url_for_name(
        os.environ["TEST_SUPERUSER_DATABASE_URL"], database_name
    )


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(
    item: pytest.Item,
    nextitem: pytest.Item | None,
) -> Generator[None, None, None]:
    # Check before other fixtures (notably monkeypatch) can undo tampering.
    _check_observer(item)
    marker = item.get_closest_marker("django_db")
    transactional = marker is not None and bool(marker.kwargs.get("transaction", False))
    if not transactional:
        yield
        return
    database_url = _test_superuser_database_url()
    triggers_present = False
    started_enabled = True
    with psycopg.connect(database_url) as raw_connection:
        rows = raw_connection.execute(
            "SELECT tgname, tgenabled FROM pg_trigger "
            "WHERE tgrelid = to_regclass('clinic_app.audit_event') "
            "AND tgname IN (%s, %s) ORDER BY tgname",
            [AUDIT_ROW_TRIGGER, AUDIT_TRUNCATE_TRIGGER],
        ).fetchall()
        if len(rows) == 2:
            triggers_present = True
            started_enabled = all(row[1] == "O" for row in rows)
            raw_connection.execute(
                f"ALTER TABLE clinic_app.audit_event ENABLE TRIGGER {AUDIT_ROW_TRIGGER}"
            )
            raw_connection.execute(
                "ALTER TABLE clinic_app.audit_event ENABLE TRIGGER "
                f"{AUDIT_TRUNCATE_TRIGGER}"
            )
            raw_connection.execute(
                "ALTER TABLE clinic_app.audit_event DISABLE TRIGGER "
                f"{AUDIT_ROW_TRIGGER}"
            )
            raw_connection.execute(
                "ALTER TABLE clinic_app.audit_event DISABLE TRIGGER "
                f"{AUDIT_TRUNCATE_TRIGGER}"
            )
    try:
        yield
    finally:
        if triggers_present:
            with psycopg.connect(database_url) as raw_connection:
                raw_connection.execute(
                    "ALTER TABLE clinic_app.audit_event ENABLE TRIGGER "
                    f"{AUDIT_ROW_TRIGGER}"
                )
                raw_connection.execute(
                    "ALTER TABLE clinic_app.audit_event ENABLE TRIGGER "
                    f"{AUDIT_TRUNCATE_TRIGGER}"
                )
                rows = raw_connection.execute(
                    "SELECT tgname, tgenabled FROM pg_trigger "
                    "WHERE tgrelid = 'clinic_app.audit_event'::regclass "
                    "AND tgname IN (%s, %s) ORDER BY tgname",
                    [AUDIT_ROW_TRIGGER, AUDIT_TRUNCATE_TRIGGER],
                ).fetchall()
            assert rows == [
                (AUDIT_ROW_TRIGGER, "O"),
                (AUDIT_TRUNCATE_TRIGGER, "O"),
            ]
        assert started_enabled is True


@pytest.fixture(scope="session")
def audit_triggers_finally_enabled(
    django_db_setup: None,
) -> Iterator[None]:
    yield
    with psycopg.connect(_test_superuser_database_url()) as raw_connection:
        raw_connection.execute(
            f"ALTER TABLE clinic_app.audit_event ENABLE TRIGGER {AUDIT_ROW_TRIGGER}"
        )
        raw_connection.execute(
            "ALTER TABLE clinic_app.audit_event ENABLE TRIGGER "
            f"{AUDIT_TRUNCATE_TRIGGER}"
        )
        rows = raw_connection.execute(
            "SELECT tgname, tgenabled FROM pg_trigger "
            "WHERE tgrelid = 'clinic_app.audit_event'::regclass "
            "AND tgname IN (%s, %s) ORDER BY tgname",
            [AUDIT_ROW_TRIGGER, AUDIT_TRUNCATE_TRIGGER],
        ).fetchall()
    assert rows == [
        (AUDIT_ROW_TRIGGER, "O"),
        (AUDIT_TRUNCATE_TRIGGER, "O"),
    ]
