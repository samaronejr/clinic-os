"""Every derived guard is proven against every input its decisions read.

Each guard runs a permitted control, every non-permitted catalog role, the
revoked/inactive/foreign-clinic states, a narrowed bundle for every permission
the control actually checked, and cells varying every actor relation (owner,
assignee, creator, run starter, preview issuer, replayed key author) that the
control actually evaluated. Relation decisions are derived and recorded by
``decision_sites``; a refused cell must be the exact denial and write nothing.
The call-removal mutant (``CLINIC_WORKFLOW_GUARD_MUTANT``) proves each guard
reaches ``has_permission``; the decision mutants (``CLINIC_WORKFLOW_DECISION_MUTANT``
in ``conftest``) prove each decision's result is observed.
"""

from __future__ import annotations

import inspect
import os
import sys
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from functools import wraps
from typing import TYPE_CHECKING, Any

import psycopg
import pytest
from apps.comms.adapters import PermanentSendError
from apps.identity import current_context
from apps.identity.models import RoleGrant, User, UserClinicRole
from apps.identity.permissions import BUNDLES_V2
from apps.workflows.access import WorkflowAccessDeniedError
from apps.workflows.validation import WorkflowConflictError, WorkflowInputError
from django.db import connection, transaction
from django.test import override_settings
from django.utils import timezone
from psycopg import sql

from database_urls import database_url_for_name
from identity.permission_support import owner_context, permission_context
from patient_service_support import runtime_role
from workflows import decision_sites
from workflows.guard_cases import (
    Cell,
    GuardCase,
    GuardWorld,
    Outcome,
    allowed_result,
    guard_cases,
    narrow,
    seed_guard_world,
)
from workflows.guard_discovery import discover_guards

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence
    from types import CodeType, FrameType
    from uuid import UUID

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)
CASES = guard_cases()
WRITTEN_TABLES = (
    "audit_event",
    "comms_integrationoperation",
    "identity_rolegrant",
    "workflows_task",
    "workflows_taskcomment",
    "workflows_workflowdefinitionversion",
    "workflows_workflowrun",
    "workflows_workflowstep",
)


class MissingPermissionWitnessError(AssertionError):
    """A declared guard did not execute its authoritative permission decision."""


@dataclass
class Observation:
    outcome: Outcome
    lines: set[int] = field(default_factory=set)
    permissions: set[str] = field(default_factory=set)
    records: list[tuple[str, bool]] = field(default_factory=list)
    wrote: bool = False


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
def _observe(target: CodeType, observation: Observation) -> Iterator[None]:
    def capture(
        execute: Callable[..., Any],
        sql: str,
        params: Sequence[object],
        many: bool,
        context: object,
    ) -> object:
        result = execute(sql, params, many, context)
        if "clinic_app.has_permission(" in sql:
            observation.permissions.add(str(params[0]))
            frame: FrameType | None = sys._getframe(1)
            while frame is not None:
                if frame.f_code is target:
                    observation.lines.add(frame.f_lineno)
                frame = frame.f_back
        return result

    with connection.execute_wrapper(capture):
        yield


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


def _pending_writes() -> int:
    """Rows inserted/updated/deleted so far by this transaction, bypassing RLS."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT coalesce(sum(n_tup_ins + n_tup_upd + n_tup_del), 0) "
            "FROM pg_stat_xact_user_tables"
        )
        row = cursor.fetchone()
    assert row is not None
    return int(row[0])


def _committed_state() -> tuple[str, ...]:
    """A superuser digest of every table a workflow decision could write."""
    url = database_url_for_name(
        os.environ["TEST_SUPERUSER_DATABASE_URL"], str(connection.settings_dict["NAME"])
    )
    with psycopg.connect(url) as raw:
        return tuple(
            str(
                raw.execute(
                    sql.SQL(
                        "SELECT md5(coalesce(string_agg(md5(t::text), '' "
                        "ORDER BY md5(t::text)), '')) FROM clinic_app.{} t"
                    ).format(sql.Identifier(table))
                ).fetchone()
            )
            for table in WRITTEN_TABLES
        )


def _decide(
    invoke: Callable[[], object],
    check: Callable[[object], bool] | None,
) -> Outcome:
    refusal: BaseException | None = None
    try:
        value = invoke()
    except (
        current_context.CurrentActorError,
        PermanentSendError,
        WorkflowConflictError,
        WorkflowInputError,
    ) as error:
        refusal = error
    if refusal is not None:
        if isinstance(refusal, WorkflowAccessDeniedError):
            # Unknown, foreign and unowned records share one payload-free denial.
            assert refusal.args == WorkflowAccessDeniedError().args
        return False, type(refusal)
    if check is not None:
        assert check(value), ("cell result check failed", value)
    return allowed_result(value), None


def _run(  # noqa: PLR0913 - one cell needs its guard, world and optional setup.
    case: GuardCase,
    world: GuardWorld,
    target: CodeType,
    invoke: Callable[[GuardWorld, Any], object],
    *,
    setup: Callable[[GuardWorld], object] | None = None,
    check: Callable[[object, Any], bool] | None = None,
) -> Observation:
    observation = Observation((False, None))
    if case.owns_transaction:
        prepared: object = world
        if setup is not None:
            with permission_context(world.graph, world.actor):
                prepared = setup(world)
        committed = _committed_state()
        with (
            runtime_role(),
            decision_sites.captured() as records,
            _observe(target, observation),
        ):
            observation.outcome = _decide(
                lambda: invoke(world, prepared),
                None if check is None else lambda value: check(value, prepared),
            )
        observation.wrote = _committed_state() != committed
    else:
        with permission_context(world.graph, world.actor):
            prepared = world if setup is None else setup(world)
            before = _pending_writes()
            with decision_sites.captured() as records, _observe(target, observation):
                observation.outcome = _decide(
                    lambda: invoke(world, prepared),
                    None if check is None else lambda value: check(value, prepared),
                )
            observation.wrote = _pending_writes() != before
            transaction.set_rollback(True)
    observation.records = records
    return observation


def _expected_denial(case: GuardCase, *, state: str = "role") -> Outcome:
    if case.predicate or case.transport:
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


def _refused(observation: Observation, expected: Outcome, label: object) -> None:
    assert observation.outcome == expected, (label, observation.outcome, expected)
    # Authorization refusals decide before any write is even attempted (aborted
    # savepoint rows count). Replay conflicts are not authority decisions.
    if not expected[0] and expected[1] not in (
        WorkflowConflictError,
        WorkflowInputError,
    ):
        assert not observation.wrote, (label, "a refusal wrote data")


def _prepare(
    world: GuardWorld, case: GuardCase, positive: str, role: str | None, **state: bool
) -> GuardWorld:
    """Give each transport cell fresh stored work before revoking/narrowing."""
    if case.refresh is not None:
        _set_role(world, positive)
        world = case.refresh(world)
    _set_role(world, role, **state)
    return world


def _grant(world: GuardWorld, role: str, permission: str) -> None:
    with owner_context(world.graph.organization_a):
        RoleGrant.objects.create(
            organization_id=world.graph.organization_a,
            clinic_id=world.clinic,
            role=role,
            permission=permission,
            bundle_version=2,
            valid_from=timezone.now() - timedelta(days=1),
        )


def _narrower(role: str, permission: str) -> Callable[[GuardWorld], object]:
    def setup(world: GuardWorld) -> object:
        narrow(world, role, permission)
        return world

    return setup


def _narrowing(
    case: GuardCase,
    world: GuardWorld,
    target: CodeType,
    positive: str,
    checked: set[str],
) -> list[str]:
    narrowable = sorted(checked & BUNDLES_V2[positive])
    rotation = sorted(
        role
        for role, permissions in BUNDLES_V2.items()
        if case.permission in permissions and set(narrowable) <= permissions
    )
    assert len(rotation) >= len(narrowable), case.symbol
    for index, permission in enumerate(narrowable):
        expected, check = case.narrowed.get(permission, (_expected_denial(case), None))
        if case.owns_transaction:
            role = rotation[index]
            world = _prepare(world, case, role, role)
            _grant(world, role, permission)
            observation = _run(
                case, world, target, lambda w, _p: case.invoke(w), check=check
            )
        else:
            _set_role(world, positive)
            observation = _run(
                case,
                world,
                target,
                lambda w, _p: case.invoke(w),
                setup=_narrower(positive, permission),
                check=check,
            )
        _refused(observation, expected, (case.symbol, "narrowed", permission))
    return narrowable


def _cell(
    case: GuardCase, world: GuardWorld, target: CodeType, cell: Cell, positive: str
) -> Observation:
    world = _prepare(world, case, positive, cell.role or positive)
    observation = _run(
        case, world, target, cell.invoke, setup=cell.setup, check=cell.check
    )
    _refused(observation, cell.expected, (case.symbol, cell.name))
    return observation


def test_guard_registry_is_derived_from_source_and_public_surfaces() -> None:
    assert {case.symbol for case in CASES} == set(discover_guards())
    assert len({case.symbol for case in CASES}) == len(CASES)
    requested = os.environ.get("CLINIC_WORKFLOW_GUARD_MUTANT")
    assert requested is None or requested in discover_guards()


def test_every_decision_site_belongs_to_a_derived_guard() -> None:
    """Fail closed: a decision outside the guard inventory has no oracle."""
    sites = decision_sites.discover_sites()
    reachable = decision_sites.reachable(set(discover_guards()))
    assert {site.symbol for site in sites.values()} <= reachable
    kinds = {site.kind for site in sites.values()}
    assert kinds == {
        decision_sites.PERMISSION,
        decision_sites.RELATION,
        decision_sites.SCOPE,
    }
    for case in CASES:
        assert case.bound <= set(sites), case.symbol


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.symbol)
@override_settings(ALLOWED_HOSTS=["testserver"])
def test_every_guard_refuses_all_nonpermitted_catalog_roles_and_reaches_permission(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    case: GuardCase,
    record_property: Callable[[str, object], None],
) -> None:
    world = seed_guard_world(rbac_graph, monkeypatch)
    with decision_sites.rewritten():
        target = inspect.unwrap(discover_guards()[case.symbol].function).__code__
        _verify_guard(case, world, target, monkeypatch, record_property)


def _verify_guard(
    case: GuardCase,
    world: GuardWorld,
    target: CodeType,
    monkeypatch: pytest.MonkeyPatch,
    record_property: Callable[[str, object], None],
) -> None:
    permitted = {
        role
        for role, permissions in BUNDLES_V2.items()
        if case.permission in permissions
    }
    denied = set(UserClinicRole.Role.values) - permitted
    assert permitted
    positive = sorted(permitted)[0]
    _set_role(world, positive)

    def invoke(w: GuardWorld, _prepared: object) -> object:
        return case.invoke(w)

    if os.environ.get("CLINIC_WORKFLOW_GUARD_MUTANT") == case.symbol:
        with _mutant(target, monkeypatch):
            control = _run(case, world, target, invoke, check=case.check)
    else:
        control = _run(case, world, target, invoke, check=case.check)
    assert control.outcome == (True, None), (case.symbol, "positive", control.outcome)
    record_property("guard_symbol", case.symbol)
    record_property("permission_witness", bool(control.lines))
    if not control.lines:
        raise MissingPermissionWitnessError(
            case.symbol, "has_permission was not executed from this guard"
        )
    for role in sorted(denied):
        world = _prepare(world, case, positive, role)
        _refused(_run(case, world, target, invoke), _expected_denial(case), role)
    for state in ("revoked", "inactive", "foreign"):
        world = _prepare(
            world,
            case,
            positive,
            None if state == "revoked" else positive,
            active=state != "inactive",
            foreign=state == "foreign",
        )
        _refused(
            _run(case, world, target, invoke),
            _expected_denial(case, state=state),
            state,
        )
    observed: defaultdict[str, set[bool]] = defaultdict(set)
    for sid, value in control.records:
        observed[sid].add(value)
    cells = [_cell(case, world, target, cell, positive) for cell in case.cells]
    for observation in cells:
        for sid, value in observation.records:
            observed[sid].add(value)
    # Every relation the guard read must be seen deciding both ways, or be a
    # declared construction-bound relation whose stored input a cell varied.
    unvaried = {
        sid
        for sid, values in observed.items()
        if values != {True, False} and sid not in case.bound
    }
    assert not unvaried, (case.symbol, "relation inputs never varied", unvaried)
    for sid in case.bound:
        assert observed[sid], (case.symbol, "bound relation never evaluated", sid)
    narrowed = _narrowing(case, world, target, positive, control.permissions)
    assert narrowed, (case.symbol, "no bundle input was varied")
    record_property("denied_catalog_roles", sorted(denied))
    record_property("permitted_control", positive)
    record_property("permission_call_lines", sorted(control.lines))
    record_property("narrowed_permissions", narrowed)
    record_property("relation_cells", [cell.name for cell in case.cells])
    record_property("relations_varied", sorted(observed))
