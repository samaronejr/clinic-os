"""A nonstaff label must not launder real role/membership authorization."""

from __future__ import annotations

import importlib.util
from typing import TYPE_CHECKING, cast

import pytest
from apps.identity.current_context import CurrentActorError, current_actor_id
from apps.identity.models import Clinic, UserClinicRole
from django.db import connection, transaction

from auth.stepup_test_support import create_role_actor
from identity import legacy_guard_inventory as discovery
from identity import test_permission_parity as parity
from identity.guard_classification import Candidate, assert_staff_coverage
from identity.permission_support import permission_context
from identity.staff_state_analysis import add_sql_staff_analysis, python_staff_analysis
from identity.test_metrics_guard_parity import METRICS_GUARDS, METRICS_SQL_ORACLES

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from uuid import UUID

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)

DIRECT = """def require_future_owner(*, clinic_id):
    actor = current_actor_id()
    if not UserClinicRole.objects.filter(
        user_id=actor, clinic_id=clinic_id, role=UserClinicRole.Role.OWNER,
    ).exists():
        raise CurrentActorError
    return actor
"""
ORM = """def require_future_owner(*, clinic_id):
    actor = current_actor_id()
    if not Clinic.objects.filter(
        pk=clinic_id, userclinicrole__user_id=actor, userclinicrole__role="owner",
    ).exists():
        raise CurrentActorError
    return actor
"""
RAW_SQL = """def require_future_owner(*, clinic_id):
    actor = current_actor_id()
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT EXISTS (SELECT 1 FROM clinic_app.identity_userclinicrole "
            "WHERE user_id=%s AND clinic_id=%s AND role='owner')",
            [actor, clinic_id],
        )
        if not cursor.fetchone()[0]:
            raise CurrentActorError
    return actor
"""
ALIASED = """from apps.identity.models import UserClinicRole as Membership

def require_future_owner(*, clinic_id):
    actor = current_actor_id()
    assignments = Membership._default_manager
    if not assignments.filter(
        user_id=actor, clinic_id=clinic_id, role="owner",
    ).exists():
        raise CurrentActorError
    return actor
"""
SQL_BODY = """def require_future_owner(*, clinic_id):
    actor = current_actor_id()
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.questionnaire_staff(%s, ARRAY['owner'])", [clinic_id],
        )
        if not cursor.fetchone()[0]:
            raise CurrentActorError
    return actor
"""
RELATED = """from apps.identity.current_context import _load_current_actor

def require_future_owner(*, clinic_id):
    actor = _load_current_actor()
    if not actor.userclinicrole_set.filter(
        clinic_id=clinic_id, role="owner",
    ).exists():
        raise CurrentActorError
    return actor.pk
"""
HELPER_ALIAS = """from apps.identity.current_context import (
    require_current_actor_clinic_roles as permitted,
)

def require_future_owner(*, clinic_id):
    return permitted(clinic_id, (UserClinicRole.Role.OWNER,))
"""
SOURCES = {
    "direct": DIRECT,
    "orm_filter": ORM,
    "raw_sql": RAW_SQL,
    "aliased_model": ALIASED,
    "resolved_sql_body": SQL_BODY,
    "related_manager": RELATED,
    "aliased_helper": HELPER_ALIAS,
}


@pytest.mark.parametrize("spelling", SOURCES)
def test_actual_membership_guard_cannot_claim_nonstaff(
    spelling: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = discovery.ROOT
    path = root / "apps/example/reviewer_role_guard.py"
    original_glob, original_read = type(path).rglob, type(path).read_text

    def files(parent: Path, pattern: str) -> object:
        yield from original_glob(parent, pattern)
        if parent == root / "apps" and pattern == "*.py":
            yield path

    def read(candidate: Path, *args: object, **kwargs: object) -> str:
        if candidate == path:
            return SOURCES[spelling]
        return original_read(candidate)

    monkeypatch.setattr(type(path), "rglob", files)
    monkeypatch.setattr(type(path), "read_text", read)
    symbol = "apps.example.reviewer_role_guard.require_future_owner"
    row: Candidate = {
        "symbol": symbol,
        "signals": discovery.discover()[symbol],
        "kind": "nonstaff",
        "reason": (
            "Synthetic attempted non-staff exemption without an executable oracle."
        ),
    }
    manifest = dict(parity.INVENTORY)
    manifest["candidates"] = [*parity.INVENTORY["candidates"], row]
    monkeypatch.setattr(parity, "INVENTORY", manifest)
    with pytest.raises(
        AssertionError,
        match=r"require_future_owner.*staff-state read requires executable authority",
    ):
        parity.test_every_authorization_candidate_is_accounted_for()


@pytest.mark.parametrize("spelling", SOURCES)
@pytest.mark.parametrize(
    "role", [UserClinicRole.Role.OWNER, UserClinicRole.Role.PHYSICIAN]
)
def test_reproducer_is_a_real_owner_only_guard(
    spelling: str,
    role: UserClinicRole.Role,
    rbac_graph: RbacGraph,
    tmp_path: Path,
) -> None:
    path = tmp_path / "synthetic_guard.py"
    path.write_text(SOURCES[spelling])
    spec = importlib.util.spec_from_file_location("synthetic_guard", path)
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
    spec.loader.exec_module(module)
    guard = cast("Callable[..., UUID]", module.require_future_owner)
    actor = create_role_actor(rbac_graph, role)
    with permission_context(rbac_graph, actor.pk):
        if role == UserClinicRole.Role.OWNER:
            assert guard(clinic_id=rbac_graph.clinic_a) == actor.pk
        else:
            with pytest.raises(CurrentActorError):
                guard(clinic_id=rbac_graph.clinic_a)


def test_nonstaff_proof_ignores_forged_signals_and_probe_names(tmp_path: Path) -> None:
    path = tmp_path / "apps/example/guard.py"
    path.parent.mkdir(parents=True)
    path.write_text(DIRECT)
    symbol = "apps.example.guard.require_future_owner"
    analysis = python_staff_analysis(tmp_path)
    add_sql_staff_analysis(analysis)
    for kind in (
        "nonstaff",
        "presentation",
        "infrastructure",
        "provider",
        "data_operation",
        "v2",
    ):
        row: Candidate = {
            "symbol": symbol,
            "signals": [],
            "kind": kind,
            "reason": "Synthetic false exemption",
            "probes": ["fake_oracle"],
        }
        with pytest.raises(
            AssertionError, match="staff-state read requires executable authority"
        ):
            assert_staff_coverage([row], analysis, {"fake_oracle"})


@pytest.mark.parametrize(
    "body",
    [
        "AS $$ SELECT classification_member() $$",
        "BEGIN ATOMIC SELECT public.classification_member(); END",
    ],
)
def test_sql_dependency_cannot_launder_membership_reads(
    body: str, tmp_path: Path
) -> None:
    path = tmp_path / "apps/example/guard.py"
    path.parent.mkdir(parents=True)
    path.write_text(
        'def guard():\n    cursor.execute("SELECT public.classification_wrapper()")\n'
    )
    symbol = "apps.example.guard.guard"
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("""
            CREATE FUNCTION public.classification_member() RETURNS boolean
            LANGUAGE sql AS $$ SELECT EXISTS (
                SELECT 1 FROM clinic_app.identity_userclinicrole WHERE role='owner'
            ) $$
        """)
        cursor.execute(
            "CREATE FUNCTION public.classification_wrapper() RETURNS boolean "
            "LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,public " + body
        )
        analysis = python_staff_analysis(tmp_path)
        add_sql_staff_analysis(analysis)
        assert "SQL table identity_userclinicrole" in analysis.evidence(symbol)
        row: Candidate = {"symbol": symbol, "signals": [], "kind": "nonstaff"}
        with pytest.raises(AssertionError):
            assert_staff_coverage([row], analysis, set())
        transaction.set_rollback(True)


def test_legitimate_operations_guards_have_no_staff_state_dependencies() -> None:
    analysis = python_staff_analysis(discovery.ROOT)
    add_sql_staff_analysis(analysis)
    for symbol in (*METRICS_GUARDS, *METRICS_SQL_ORACLES):
        assert symbol in analysis.direct
        assert not analysis.evidence(symbol), symbol
