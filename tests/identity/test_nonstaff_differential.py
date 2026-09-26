"""Equivalent spellings must fail by observed decisions, not syntax."""

from __future__ import annotations

import importlib.util
import sys
from types import ModuleType
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import pytest
from apps.identity.current_context import CurrentActorError, current_actor_id
from apps.identity.models import Clinic, User, UserClinicRole
from django.db import connection

from identity.guard_classification import Candidate, assert_staff_coverage
from identity.legacy_guard_inventory import ROOT
from identity.nonstaff_differential import (
    STAFF_STATES,
    DifferentialProbe,
    StaffDependentError,
    assert_behavioral_classifications,
)
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
    # This is the same behavioural gate used by the census fixture. There is
    # deliberately no static-analysis call on this primary rejection path.
    with pytest.raises(StaffDependentError) as refused:
        assert_behavioral_classifications(
            [row],
            {SYMBOL: [probe]},
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
    with pytest.raises(StaffDependentError):
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
            clinic=rbac_graph.clinic_a,
            organization=rbac_graph.organization_a,
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
            clinic=rbac_graph.clinic_a,
            organization=rbac_graph.organization_a,
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
            clinic=rbac_graph.clinic_a,
            organization=rbac_graph.organization_a,
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
            clinic=rbac_graph.clinic_a,
            organization=rbac_graph.organization_a,
        )
