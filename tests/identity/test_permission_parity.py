"""Legacy parity at actual callable boundaries, with a reviewed broad census."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, cast

import pytest
from apps.identity import stepup
from apps.identity.models import UserClinicRole
from apps.tenancy.db import tenant_context
from django.db import DatabaseError, transaction
from django.test import override_settings

from auth.stepup_test_support import STEP_UP_NOW
from identity import legacy_guard_inventory
from identity import legacy_operational_boundaries as operational
from identity import legacy_prescription_boundaries as prescriptions
from identity import legacy_sql_boundaries as sql_boundaries
from identity import legacy_teleconsult_boundaries as teleconsult
from identity import legacy_view_boundaries as views
from identity.guard_classification import Candidate, assert_staff_coverage
from identity.legacy_clinical_boundaries import BOUNDARIES as CLINICAL_BOUNDARIES
from identity.legacy_guard_inventory import declared_probes, discover
from identity.legacy_identity_boundaries import BOUNDARIES as IDENTITY_BOUNDARIES
from identity.legacy_owner_boundaries import BOUNDARIES as OWNER_BOUNDARIES
from identity.legacy_parity_support import LEGACY, exercise, target_code, world
from identity.legacy_predicate_boundaries import BOUNDARIES as PREDICATE_BOUNDARIES
from identity.legacy_scope_boundaries import BOUNDARIES as SCOPE_BOUNDARIES
from identity.legacy_sql_inventory import SqlInventoryEntry, assert_sql_inventory
from identity.legacy_tenant_boundaries import exercise_tenant_boundaries
from identity.nonstaff_census import run_nonstaff_census
from identity.permission_support import owner_context
from identity.sql_guard_probes import ALL_ROLES, PROBES, call, seed_sql_world
from identity.staff_state_analysis import add_sql_staff_analysis, python_staff_analysis
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from collections.abc import Callable

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)
CORE = (
    *IDENTITY_BOUNDARIES,
    *CLINICAL_BOUNDARIES,
    *PREDICATE_BOUNDARIES,
    *SCOPE_BOUNDARIES,
    *OWNER_BOUNDARIES,
)


class Inventory(TypedDict):
    schema_version: int
    candidates: list[Candidate]
    probes: list[str]
    method: list[str]
    sql_guards: dict[str, SqlInventoryEntry]


INVENTORY = cast(
    "Inventory", json.loads(Path(__file__).with_name("legacy_guards.json").read_text())
)


@pytest.fixture
def nonstaff_differential_census(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    record_property: Callable[[str, object], None],
) -> None:
    receipts = run_nonstaff_census(INVENTORY["candidates"], rbac_graph, monkeypatch)
    record_property(
        "nonstaff_staff_state_decisions", json.dumps(receipts, sort_keys=True)
    )


@pytest.mark.usefixtures("nonstaff_differential_census")
def test_every_authorization_candidate_is_accounted_for() -> None:
    assert INVENTORY["schema_version"] == 2
    candidates = INVENTORY["candidates"]
    assert len({row["symbol"] for row in candidates}) == len(candidates)
    assert discover() == {row["symbol"]: row["signals"] for row in candidates}
    declared = declared_probes()
    assert sorted(declared) == INVENTORY["probes"]
    sql_oracles = {f"clinic_app.{probe.name}" for probe in PROBES.values()}
    bases = {probe.split("#", 1)[0] for probe in declared} | sql_oracles
    for row in candidates:
        if row["kind"] == "direct":
            assert "probes" in row
            assert row["symbol"] in bases
            assert set(row["probes"]) <= declared
        elif row["kind"] == "polymorphic":
            assert "probes" in row
            assert row["probes"]
            assert all(
                target_code(probe) is target_code(row["symbol"])
                for probe in row["probes"]
            )
        elif row["kind"] == "delegated":
            assert "enforced_by" in row
            assert row["enforced_by"]
            assert set(row["enforced_by"]) <= bases
        else:
            assert row["kind"] in {
                "nonstaff",
                "provider",
                "infrastructure",
                "v2",
                "presentation",
                "data_operation",
            }
            assert "reason" in row
            assert row["reason"]
    # All definitions using the canonical role helper are directly exercised;
    # callers cannot again be replaced with tests of a harvested role tuple.
    for row in candidates:
        if "role_helper" in row["signals"]:
            assert row["kind"] in {"direct", "polymorphic"}, row["symbol"]
    assert_sql_inventory(INVENTORY["sql_guards"])
    analysis = python_staff_analysis(legacy_guard_inventory.ROOT)
    add_sql_staff_analysis(analysis)
    assert_staff_coverage(candidates, analysis, declared | bases | sql_oracles)


@pytest.fixture(autouse=True)
def fixed_verification_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stepup, "_utc_now_seconds", lambda: STEP_UP_NOW)


@pytest.mark.parametrize("legacy_role", LEGACY)
@pytest.mark.parametrize("family", sorted({boundary.family for boundary in CORE}))
def test_actual_legacy_boundaries(
    rbac_graph: RbacGraph, legacy_role: str, family: str
) -> None:
    subject = world(rbac_graph, legacy_role)
    if family == "organization":
        with owner_context(rbac_graph.organization_a):
            UserClinicRole.objects.get_or_create(
                organization_id=rbac_graph.organization_a,
                clinic_id=rbac_graph.clinic_b,
                user_id=subject.actor.pk,
                role=legacy_role,
            )
    for boundary in CORE:
        if boundary.family == family:
            exercise(boundary, subject)


@pytest.mark.parametrize("legacy_role", LEGACY)
@pytest.mark.parametrize(
    "family", ["operational", "prescription", "http", "teleconsult", "sql"]
)
def test_actual_domain_boundaries(
    rbac_graph: RbacGraph,
    legacy_role: str,
    family: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject = world(rbac_graph, legacy_role)
    with override_settings(
        BILLING_SYNTHETIC_PIX=True,
        PRESCRIPTION_SYNTHETIC_SIGNING=True,
        PHYSICIAN_SYNTHETIC_REGISTRY=True,
        TELECONSULT_SYNTHETIC_PROVIDER=True,
    ):
        op = operational.seed_operational(subject)
        if family == "operational":
            selected = operational.boundaries(op)
        elif family == "sql":
            selected = sql_boundaries.boundaries(op)
        elif family == "teleconsult":
            selected = teleconsult.boundaries(
                teleconsult.seed_teleconsult(subject, op, monkeypatch)
            )
        else:
            rx = prescriptions.seed_prescription(subject)
            selected = (
                prescriptions.boundaries(rx)
                if family == "prescription"
                else views.boundaries(op, rx)
            )
        for boundary in selected:
            exercise(boundary, subject)


@pytest.mark.parametrize("legacy_role", LEGACY)
def test_actual_tenant_boundaries(rbac_graph: RbacGraph, legacy_role: str) -> None:
    exercise_tenant_boundaries(world(rbac_graph, legacy_role))


@pytest.mark.parametrize("role", ALL_ROLES)
@override_settings(BILLING_SYNTHETIC_PIX=True, TELECONSULT_SYNTHETIC_PROVIDER=True)
def test_sql_guards_allow_and_deny_all_staff_roles(
    rbac_graph: RbacGraph,
    role: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject = seed_sql_world(rbac_graph, role, monkeypatch)
    with (
        runtime_role(),
        tenant_context(subject.actor.actor.pk, rbac_graph.organization_a),
    ):
        for name, probe in PROBES.items():
            for valid in (True, False):
                expected = valid and role in probe.roles
                if not expected and probe.refusal_state is not None:
                    with pytest.raises(DatabaseError) as caught, transaction.atomic():
                        call(probe, subject, valid)
                    assert (
                        getattr(caught.value.__cause__, "sqlstate", None)
                        == probe.refusal_state
                    ), name
                else:
                    with transaction.atomic():
                        actual = call(probe, subject, valid)
                        transaction.set_rollback(True)
                    assert actual is expected, (name, role, valid, actual)
