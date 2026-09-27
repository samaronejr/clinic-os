"""Every derived guard gets real allowed/refused roles and execution witnesses.

The mutation runner removes permission evaluation only while one named target
executes. PostgreSQL/RLS and domain effects stay real: independent DB defenses
cannot conceal the loss of a service's explicit permission delegation.
"""

from __future__ import annotations

import inspect
import os
import sys
from contextlib import contextmanager
from functools import wraps
from typing import TYPE_CHECKING, Any

import pytest
from apps.comms.adapters import PermanentSendError
from apps.identity import current_context
from apps.identity.models import User, UserClinicRole
from apps.identity.permissions import BUNDLES_V2
from apps.workflows.access import WorkflowAccessDeniedError
from django.db import connection, transaction
from django.test import override_settings

from identity.permission_support import owner_context, permission_context
from patient_service_support import runtime_role
from workflows.guard_cases import (
    GuardCase,
    GuardWorld,
    allowed_result,
    guard_cases,
    seed_guard_world,
)
from workflows.guard_discovery import discover_guards

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from types import CodeType, FrameType
    from uuid import UUID

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)
CASES = guard_cases()


class MissingPermissionWitnessError(AssertionError):
    """A declared guard did not execute its authoritative permission decision."""


def _set_role(
    world: GuardWorld, role: str | None, *, active: bool = True, foreign: bool = False
) -> None:
    User.objects.filter(pk=world.actor).update(is_active=active)
    with owner_context(world.graph.organization_a):
        UserClinicRole.objects.filter(user_id=world.actor).delete()
        if role is not None:
            UserClinicRole.objects.create(
                organization_id=world.graph.organization_a,
                clinic_id=world.graph.clinic_b if foreign else world.clinic,
                user_id=world.actor,
                role=role,
            )
    world.request.session.modified = False


@contextmanager
def _observe(target: CodeType) -> Iterator[set[int]]:
    lines: set[int] = set()

    def capture(
        execute: Callable[..., Any],
        sql: str,
        params: object,
        many: bool,
        context: object,
    ) -> object:
        result = execute(sql, params, many, context)
        if "clinic_app.has_permission(" in sql:
            frame: FrameType | None = sys._getframe(1)
            while frame is not None:
                if frame.f_code is target:
                    lines.add(frame.f_lineno)
                frame = frame.f_back
        return result

    with connection.execute_wrapper(capture):
        yield lines


@contextmanager
def _mutant(target: CodeType, patch: pytest.MonkeyPatch) -> Iterator[None]:
    original = current_context.require_permission

    @wraps(original)
    def stripped(
        permission: str, *, clinic_id: UUID, patient_enrollment_id: UUID | None = None
    ) -> current_context.UserId:
        frame: FrameType | None = sys._getframe(1)
        while frame is not None:
            if frame.f_code is target:
                return current_context.current_actor_id()
            frame = frame.f_back
        return original(
            permission, clinic_id=clinic_id, patient_enrollment_id=patient_enrollment_id
        )

    with patch.context() as local:
        for name, module in tuple(sys.modules.items()):
            if name.startswith("apps."):
                for attribute, value in tuple(vars(module).items()):
                    if value is original:
                        local.setattr(module, attribute, stripped)
        yield


def _decision(
    case: GuardCase, world: GuardWorld
) -> tuple[bool, type[BaseException] | None]:
    try:
        value = case.invoke(world)
    except (current_context.CurrentActorError, PermanentSendError) as error:
        return False, type(error)
    return allowed_result(value), None


def _exercise(
    case: GuardCase, world: GuardWorld, target: CodeType
) -> tuple[tuple[bool, type[BaseException] | None], set[int]]:
    if case.owns_transaction:
        with runtime_role(), _observe(target) as lines:
            outcome = _decision(case, world)
    else:
        with permission_context(world.graph, world.actor), _observe(target) as lines:
            outcome = _decision(case, world)
            transaction.set_rollback(True)
    return outcome, lines


def _expected_denial(
    case: GuardCase, *, state: str = "role"
) -> tuple[bool, type[BaseException] | None]:
    if case.predicate or case.transport or case.symbol == "apps.workflows.views.tasks":
        return False, None
    if case.symbol == "apps.workflows.external.SyntheticWorkflowAdapter.prepare":
        return False, PermanentSendError
    if state != "role" and case.symbol in {
        "apps.workflows.engine._locked_step",
        "apps.workflows.engine.claim_step",
        "apps.workflows.engine.apply_claim",
        "apps.workflows.engine._apply_or_fail",
    }:
        return False, WorkflowAccessDeniedError
    return (
        False,
        current_context._UnavailableActorError
        if state == "inactive"
        else current_context._UnauthorizedActorError,
    )


def test_guard_registry_is_derived_from_source_and_public_surfaces() -> None:
    assert {case.symbol for case in CASES} == set(discover_guards())
    assert len({case.symbol for case in CASES}) == len(CASES)
    requested = os.environ.get("CLINIC_WORKFLOW_GUARD_MUTANT")
    assert requested is None or requested in discover_guards()


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.symbol)
@override_settings(ALLOWED_HOSTS=["testserver"])
def test_every_guard_refuses_all_nonpermitted_catalog_roles_and_reaches_permission(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    case: GuardCase,
    record_property: Callable[[str, object], None],
) -> None:
    guards = discover_guards()
    target = inspect.unwrap(guards[case.symbol].function).__code__
    world = seed_guard_world(rbac_graph, monkeypatch)
    permitted = {
        role
        for role, permissions in BUNDLES_V2.items()
        if case.permission in permissions
    }
    denied = set(UserClinicRole.Role.values) - permitted
    assert permitted
    positive = sorted(permitted)[0]
    _set_role(world, positive)
    if os.environ.get("CLINIC_WORKFLOW_GUARD_MUTANT") == case.symbol:
        with _mutant(target, monkeypatch):
            control, lines = _exercise(case, world, target)
    else:
        control, lines = _exercise(case, world, target)
    assert control == (True, None), (case.symbol, "invalid positive control", control)
    record_property("guard_symbol", case.symbol)
    record_property("permission_witness", bool(lines))
    if not lines:
        raise MissingPermissionWitnessError(
            case.symbol, "has_permission was not executed from this guard"
        )
    for role in sorted(denied):
        _set_role(world, role)
        result, _lines = _exercise(case, world, target)
        assert result == _expected_denial(case), (case.symbol, role, result)
    for state in ("revoked", "inactive", "foreign"):
        _set_role(
            world,
            None if state == "revoked" else positive,
            active=state != "inactive",
            foreign=state == "foreign",
        )
        result, _lines = _exercise(case, world, target)
        assert result == _expected_denial(case, state=state), (
            case.symbol,
            state,
            result,
        )
    record_property("denied_catalog_roles", sorted(denied))
    record_property("permitted_control", positive)
    record_property("permission_call_lines", sorted(lines))
