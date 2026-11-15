"""Reproduce the stale tenant estimate that stalled the canonical exact search."""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any, cast

import pytest
from apps.intake.models import PatientClinicEnrollment
from apps.intake.patient_name_index import name_indexes
from apps.tenancy.db import tenant_context
from django.db import connection
from ops.perf import dataset
from ops.perf.profiles import profile_named
from ops.perf.runtime import runtime_role

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from ops.perf.profiles import Profile


def _plan(tenant: dataset.Tenant) -> dict[str, Any]:
    with runtime_role(), tenant_context(tenant.admin_id, tenant.organization_id):
        matches = (
            PatientClinicEnrollment.objects.filter(
                clinic_id=tenant.clinic_id,
                patient__full_name_index__in=name_indexes(tenant.sample_names[-1]),
            )
            .select_related("patient")
            .order_by("id")[:25]
        )
        statement, parameters = matches.query.sql_with_params()
        with connection.cursor() as cursor:
            cursor.execute(
                "EXPLAIN (ANALYZE, TIMING FALSE, FORMAT JSON) " + statement,
                parameters,
            )
            row = cursor.fetchone()
    assert row is not None
    value = cast("list[dict[str, Any]]", row[0])
    return cast("dict[str, Any]", value[0]["Plan"])


def _nodes(plan: dict[str, Any]) -> list[dict[str, Any]]:
    result = [plan]
    for child in plan.get("Plans", []):
        result.extend(_nodes(child))
    return result


@pytest.mark.django_db(transaction=True)
def test_seed_refreshes_stale_statistics_before_exact_search(
    synthetic_secret_backend: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Populate and analyze an older cohort first, as in the retained canonical DB.
    profile = dataclasses.replace(
        profile_named("P-small"), organizations=2, patients_per_org=40
    )
    dataset.seed_dataset(profile, seed=71)
    seeded: list[dataset.Tenant] = []
    seed = dataset.seed_tenant

    def capture_tenant(profile: Profile, *, index: int) -> dataset.Tenant:
        tenant = seed(profile, index=index)
        seeded.append(tenant)
        return tenant

    monkeypatch.setattr(dataset, "seed_tenant", capture_tenant)
    before: list[dict[str, Any]] = []

    def before_analyze(
        execute: Callable[..., object],
        sql: str,
        params: Sequence[object] | None,
        many: bool,
        context: dict[str, object],
    ) -> object:
        if sql.startswith("ANALYZE clinic_app.intake_patient,"):
            before.append(_plan(seeded[0]))
        return execute(sql, params, many, context)

    with connection.execute_wrapper(before_analyze):
        current = dataset.seed_dataset(profile, seed=72)
    assert len(before) == 1
    old_patient_scan = next(
        node
        for node in _nodes(before[0])
        if node.get("Relation Name") == "intake_patient"
    )
    new_patient_scan = next(
        node
        for node in _nodes(_plan(current.focus))
        if node.get("Relation Name") == "intake_patient"
    )
    assert old_patient_scan["Actual Loops"] == profile.patients_per_org
    assert "full_name_index" not in old_patient_scan.get("Index Cond", "")
    assert new_patient_scan["Actual Loops"] == 1
    assert "full_name_index" in new_patient_scan["Index Cond"]
