"""Equivalent spellings must fail by observed decisions, not syntax."""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import replace
from types import ModuleType
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import pytest
from apps.identity import service_principals
from apps.identity.current_context import CurrentActorError, current_actor_id
from apps.identity.models import Clinic, User, UserClinicRole
from django.db import connection

from identity.authority_observer import AuthorityObservedError
from identity.guard_classification import Candidate, assert_staff_coverage
from identity.legacy_guard_inventory import ROOT
from identity.nonstaff_differential import (
    STAFF_STATES,
    DifferentialProbe,
    StaffDependentError,
    assert_behavioral_classifications,
    assert_staff_invariant,
)
from identity.nonstaff_states import ReplayScope
from identity.staff_state_analysis import add_sql_staff_analysis, python_staff_analysis
from identity.test_guard_classification import SOURCES

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)

REEXPORT = """from apps.example.authority import permitted
from apps.identity.models import UserClinicRole

def require_future_owner(*, clinic_id):
    return permitted(clinic_id, (UserClinicRole.Role.OWNER,))
"""
BOUND_METHOD = """from apps.identity.current_context import _load_current_actor

def require_future_owner(*, clinic_id):
    actor = _load_current_actor()
    if not getattr(actor, "_has_" + "role")("owner"):
        raise CurrentActorError
    return actor.pk
"""
SPELLINGS = {**SOURCES, "reexport": REEXPORT, "computed_bound_method": BOUND_METHOD}
SYMBOL = "apps.example.reviewer_role_guard.require_future_owner"


def load_reproducer(
    spelling: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Callable[..., object]:
    package = ModuleType("apps.example")
    package.__path__ = []
    monkeypatch.setitem(sys.modules, package.__name__, package)
    sources = {
        "authority": (
            "from apps.identity.current_context import "
            "require_current_actor_clinic_roles as permitted\n"
        ),
        "reviewer_role_guard": SPELLINGS[spelling],
    }
    folder = tmp_path / "apps/example"
    folder.mkdir(parents=True, exist_ok=True)
    for name, source in sources.items():
        path = folder / (name + ".py")
        path.write_text(source)
        qualified = "apps.example." + name
        spec = importlib.util.spec_from_file_location(qualified, path)
        assert spec is not None
        assert spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        module.__dict__.update(
            UserClinicRole=UserClinicRole,
            Clinic=Clinic,
            connection=connection,
            current_actor_id=current_actor_id,
            CurrentActorError=CurrentActorError,
        )
        monkeypatch.setitem(sys.modules, qualified, module)
        spec.loader.exec_module(module)
    return cast("Callable[..., object]", module.require_future_owner)


@pytest.mark.parametrize("kind", ["nonstaff", "infrastructure"])
@pytest.mark.parametrize("spelling", SPELLINGS)
def test_behaviour_refuses_every_role_guard_spelling(
    spelling: str,
    kind: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rbac_graph: RbacGraph,
) -> None:
    guard = load_reproducer(spelling, tmp_path, monkeypatch)
    actor = User.objects.create(username="synthetic-differential-" + uuid4().hex)
    probe = DifferentialProbe(SYMBOL, lambda: guard(clinic_id=rbac_graph.clinic_a))
    row: Candidate = {
        "symbol": SYMBOL,
        "kind": kind,
        "signals": [],
        "reason": "Synthetic attempted exemption",
    }
    # The certificate refuses the observed channel before the backstop runs.
    with pytest.raises(AuthorityObservedError) as observed:
        assert_behavioral_classifications(
            [row],
            {SYMBOL: [probe]},
            actor=actor,
            scope=ReplayScope(rbac_graph.clinic_a, rbac_graph.organization_a),
        )
    assert observed.value.args[0] == SYMBOL
    assert observed.value.args[1]
    # Retain the real singleton/union refusal oracle independently.
    with pytest.raises(StaffDependentError) as refused:
        assert_staff_invariant(
            probe,
            actor=actor,
            clinic=rbac_graph.clinic_a,
            organization=rbac_graph.organization_a,
        )
    symbol, decisions = refused.value.args
    assert symbol == SYMBOL
    assert decisions == {role: role == "owner" for role in STAFF_STATES}


def test_computed_method_is_detected_even_when_static_analysis_misses_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rbac_graph: RbacGraph,
) -> None:
    guard = load_reproducer("computed_bound_method", tmp_path, monkeypatch)
    analysis = python_staff_analysis(ROOT)
    extra = python_staff_analysis(tmp_path)
    analysis.direct.update(extra.direct)
    analysis.calls.update(extra.calls)
    add_sql_staff_analysis(analysis)
    assert not analysis.evidence(SYMBOL)
    actor = User.objects.create(username="synthetic-opaque-" + uuid4().hex)
    row: Candidate = {"symbol": SYMBOL, "kind": "nonstaff", "signals": []}
    assert_staff_coverage([row], analysis, set())
    with pytest.raises(AuthorityObservedError):
        assert_behavioral_classifications(
            [row],
            {
                SYMBOL: [
                    DifferentialProbe(
                        SYMBOL, lambda: guard(clinic_id=rbac_graph.clinic_a)
                    )
                ]
            },
            actor=actor,
            scope=ReplayScope(rbac_graph.clinic_a, rbac_graph.organization_a),
        )


def test_static_backstop_follows_transitive_reexports(tmp_path: Path) -> None:
    folder = tmp_path / "apps/example"
    folder.mkdir(parents=True)
    (folder / "__init__.py").write_text(
        "from .authority import permitted as exported\n"
    )
    (folder / "authority.py").write_text(
        "from apps.identity.current_context import "
        "require_current_actor_clinic_roles as original\n"
        "permitted = original\n"
    )
    (folder / "reviewer_role_guard.py").write_text(
        REEXPORT.replace(
            "from apps.example.authority import permitted",
            "from apps.example import exported as permitted",
        )
    )
    analysis = python_staff_analysis(ROOT)
    extra = python_staff_analysis(tmp_path)
    analysis.direct.update(extra.direct)
    analysis.calls.update(extra.calls)
    assert analysis.evidence(SYMBOL)
    row: Candidate = {"symbol": SYMBOL, "kind": "nonstaff", "signals": []}
    with pytest.raises(AssertionError):
        assert_staff_coverage([row], analysis, set())


@pytest.mark.parametrize("kind", ["nonstaff", "infrastructure"])
def test_unregistered_exemption_fails_closed(rbac_graph: RbacGraph, kind: str) -> None:
    actor = User.objects.create(username="synthetic-missing-" + uuid4().hex)
    row: Candidate = {
        "symbol": SYMBOL,
        "kind": kind,
        "signals": [],
        "differential": False,
    }
    with pytest.raises(AssertionError):
        assert_behavioral_classifications(
            [row],
            {},
            actor=actor,
            scope=ReplayScope(rbac_graph.clinic_a, rbac_graph.organization_a),
        )


def test_adapter_must_enter_the_named_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rbac_graph: RbacGraph,
) -> None:
    load_reproducer("reexport", tmp_path, monkeypatch)
    actor = User.objects.create(username="synthetic-entry-" + uuid4().hex)
    row: Candidate = {"symbol": SYMBOL, "kind": "nonstaff", "signals": []}
    with pytest.raises(AssertionError) as rejected:
        assert_behavioral_classifications(
            [row],
            {SYMBOL: [DifferentialProbe(SYMBOL, lambda: True)]},
            actor=actor,
            scope=ReplayScope(rbac_graph.clinic_a, rbac_graph.organization_a),
        )
    assert rejected.value.args[0] == (SYMBOL, "target_not_entered")


def test_broken_invocation_cannot_be_counted_as_authorization_denial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rbac_graph: RbacGraph,
) -> None:
    guard = load_reproducer("reexport", tmp_path, monkeypatch)
    actor = User.objects.create(username="synthetic-error-" + uuid4().hex)
    row: Candidate = {"symbol": SYMBOL, "kind": "nonstaff", "signals": []}
    with pytest.raises(TypeError):
        assert_behavioral_classifications(
            [row],
            {SYMBOL: [DifferentialProbe(SYMBOL, guard)]},
            actor=actor,
            scope=ReplayScope(rbac_graph.clinic_a, rbac_graph.organization_a),
        )


def test_delegated_label_without_the_named_call_fails_statically() -> None:
    # A symbol with no staff read cannot keep a delegation it does not make.
    analysis = python_staff_analysis(ROOT)
    add_sql_staff_analysis(analysis)
    symbol = "apps.ehr.finalization._next_version"
    assert not analysis.evidence(symbol)
    row: Candidate = {
        "symbol": symbol,
        "kind": "delegated",
        "signals": [],
        "enforced_by": ["clinic_app.has_permission"],
    }
    with pytest.raises(AssertionError) as rejected:
        assert_staff_coverage([row], analysis, {"clinic_app.has_permission"})
    assert rejected.value.args[0][:2] == (symbol, "unresolved delegation")


def test_root_delegation_must_be_reached_when_executed(rbac_graph: RbacGraph) -> None:
    # On the runtime role the owner-connection check refuses first, so the
    # named has_permission root never runs: the label is refused.
    symbol = "apps.identity.service_principals.revoke_principal"
    actor = User.objects.create(username="synthetic-delegation-" + uuid4().hex)
    row: Candidate = {
        "symbol": symbol,
        "kind": "delegated",
        "signals": ["denial"],
        "enforced_by": ["clinic_app.has_permission"],
    }
    probe = DifferentialProbe(
        symbol,
        lambda: service_principals.revoke_principal(
            clinic_id=rbac_graph.clinic_a, principal_id=uuid4()
        ),
        expected=False,
    )
    scope = ReplayScope(rbac_graph.clinic_a, rbac_graph.organization_a)
    with pytest.raises(AssertionError) as rejected:
        assert_behavioral_classifications(
            [row], {symbol: [probe]}, actor=actor, scope=scope
        )
    assert rejected.value.args[0][:2] == (symbol, "delegation_not_observed")
    owner_probe = replace(probe, database_role="clinic_owner")
    report = assert_behavioral_classifications(
        [row], {symbol: [owner_probe]}, actor=actor, scope=scope
    )
    assert ("function", "clinic_app.has_permission") in report.observations[symbol]
