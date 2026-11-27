"""The inbox verifier must not inherit the harness's staff session state.

``migration_upgrade_verify.py`` holds ``app.current_user_id`` and
``app.current_tenant`` at session scope. The inbound resolver refuses a session
that carries a staff actor, so a case that reaches it before any staff call
(which clears that state when its tenant context ends) is rejected. Which case
runs first used to depend on the row order of an unordered query, so the failure
appeared in some runs only. These tests force each such case to run first.
"""

import ast
import inspect
import textwrap
from collections.abc import Callable
from contextlib import nullcontext
from types import SimpleNamespace
from typing import Self
from uuid import UUID

import pytest
from apps.comms.inbox_inbound import InboundRejectedError
from apps.tenancy import db as tenancy_db
from ops.testing import migration_upgrade_inbox_verify as verifier
from ops.testing.migration_upgrade_inbox_fixture import CASES

# The session settings the resolver requires to be empty
# (apps/comms/migrations/_mac_proof_sql.py, comms_resolve_inbound).
RESOLVER_REFUSES = (
    "app.current_user_id",
    "app.current_patient_session",
    "app.current_principal",
)


def _world_methods(*markers: str) -> frozenset[str]:
    return frozenset(
        name
        for name, method in inspect.getmembers(verifier._World, inspect.isfunction)
        if any(marker in inspect.getsource(method) for marker in markers)
    )


# A staff call runs under ``tenant_context``, whose end clears the session state.
CLEARS = _world_methods("tenant_context(", "self.staff(")
RESOLVES = _world_methods("receive_inbound(")


class _Reach(ast.NodeVisitor):
    """The world methods a case calls, in evaluation order, through its helpers."""

    def __init__(self) -> None:
        self.reached: list[str] = []

    def visit_Call(self, node: ast.Call) -> None:
        self.generic_visit(node)
        target = node.func
        if (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == "world"
        ):
            self.reached.append(target.attr)
        elif (
            isinstance(target, ast.Name)
            and node.args
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == "world"
            and inspect.isfunction(getattr(verifier, target.id, None))
        ):
            self.reached += _interactions(getattr(verifier, target.id))


def _interactions(function: Callable[..., object]) -> list[str]:
    visitor = _Reach()
    visitor.visit(ast.parse(textwrap.dedent(inspect.getsource(function))))
    return visitor.reached


def _up_to_the_resolver(function: Callable[..., object]) -> list[str] | None:
    """The calls before the resolver is reached, or None if a staff call comes first."""
    before: list[str] = []
    for name in _interactions(function):
        if name in CLEARS:
            return None
        before.append(name)
        if name in RESOLVES:
            return before
    return None


INBOUND_FIRST = sorted(
    case
    for case, function in verifier._CASES.items()
    if _up_to_the_resolver(function) is not None
)


def test_the_cases_are_the_fixtures_cases() -> None:
    assert {case for case, _, _ in CASES} == set(verifier._CASES)


def test_every_known_inbound_first_case_is_found() -> None:
    # Found by `late_failure` before a staff call, in the failing runs.
    assert {"skew", "tie", "e_two_dispatch"} <= set(INBOUND_FIRST)


class _Cursor:
    def __init__(self, session: "_Session") -> None:
        self.session = session

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exception: object) -> None:
        return None

    def execute(self, sql: str) -> None:
        for statement in sql.split(";"):
            statement = statement.strip()  # noqa: PLW2901
            if statement.startswith("RESET "):
                self.session.settings.pop(statement.removeprefix("RESET "), None)


class _Session:
    """One connection's session-scoped settings, as the harness leaves them."""

    connection = object()

    def __init__(self) -> None:
        # migration_upgrade_verify.py sets both with set_config(..., false).
        self.settings = {
            "app.current_user_id": str(UUID(int=1)),
            "app.current_tenant": str(UUID(int=2)),
        }

    def cursor(self) -> _Cursor:
        return _Cursor(self)


class _ModelWorld:
    """The world's effect on session state, with no database behind it."""

    def __init__(self, session: _Session) -> None:
        self.session = session

    def __getattr__(self, name: str) -> Callable[..., int]:
        def call(*_arguments: object, **_keywords: object) -> int:
            if name in CLEARS:
                tenancy_db.clear_connection_tenant_gucs()
            elif name in RESOLVES and any(
                self.session.settings.get(setting) for setting in RESOLVER_REFUSES
            ):
                raise InboundRejectedError
            return 1

        return call


def _probe(
    function: Callable[..., object],
) -> Callable[[object, dict[str, str], list[str]], None]:
    calls = _up_to_the_resolver(function) or []

    def probe(world: object, _ids: dict[str, str], _failures: list[str]) -> None:
        for name in calls:
            getattr(world, name)()

    return probe


@pytest.mark.parametrize("first", INBOUND_FIRST)
def test_a_case_that_reaches_the_resolver_first_is_not_refused(
    monkeypatch: pytest.MonkeyPatch, first: str
) -> None:
    session = _Session()
    monkeypatch.setattr(tenancy_db, "connection", session)
    monkeypatch.setattr(
        tenancy_db, "transaction", SimpleNamespace(atomic=lambda **_: nullcontext())
    )
    monkeypatch.setattr(
        verifier, "_World", lambda _markers, _inbox: _ModelWorld(session)
    )
    order = [first, *(case for case in verifier._CASES if case != first)]
    monkeypatch.setattr(
        verifier,
        "_CASES",
        {case: _probe(verifier._CASES[case]) for case in order},
    )
    cases: dict[str, dict[str, str]] = {case: {} for case in order}
    markers: dict[str, object] = {"inbox": {"cases": cases}}

    assert next(iter(cases)) == first
    assert verifier.verify_inbox(markers) == []
