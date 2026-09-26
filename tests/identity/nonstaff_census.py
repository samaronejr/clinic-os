"""Primary exemption gate, executed before the source/catalog census accepts it."""

from __future__ import annotations

from typing import TYPE_CHECKING

from apps.identity import stepup
from django.test import override_settings
from django.utils import timezone

from auth.stepup_test_support import STEP_UP_NOW
from identity.nonstaff_differential import (
    DifferentialProbe,
    assert_behavioral_classifications,
)
from identity.nonstaff_infrastructure_probes import (
    infrastructure_probes,
    metrics_probes,
)
from identity.nonstaff_patient_probes import patient_probes
from identity.nonstaff_subjects import seed_nonstaff

if TYPE_CHECKING:
    from collections.abc import Sequence

    import pytest

    from identity.guard_classification import Candidate
    from rbac_fixtures import RbacGraph


def run_nonstaff_census(
    candidates: Sequence[Candidate], graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> dict[str, list[tuple[bool, ...]]]:
    stamp = timezone.now()
    with (
        monkeypatch.context() as patch,
        override_settings(
            BILLING_SYNTHETIC_PIX=True,
            PRESCRIPTION_SYNTHETIC_SIGNING=True,
            PHYSICIAN_SYNTHETIC_REGISTRY=True,
            TELECONSULT_SYNTHETIC_PROVIDER=True,
            ALLOWED_HOSTS=["testserver"],
        ),
    ):
        patch.setattr(stepup, "_utc_now_seconds", lambda: STEP_UP_NOW)
        patch.setattr(timezone, "now", lambda: stamp)
        data = seed_nonstaff(graph, patch)
        probes: dict[str, list[DifferentialProbe]] = {}
        for probe in [
            *patient_probes(data),
            *infrastructure_probes(data, patch),
            *metrics_probes(data, patch),
        ]:
            probes.setdefault(probe.symbol, []).append(probe)
        return assert_behavioral_classifications(
            candidates,
            probes,
            actor=data.actor,
            clinic=graph.clinic_a,
            organization=graph.organization_a,
        )
