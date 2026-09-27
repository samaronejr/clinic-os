"""Primary exemption gate, executed before the source/catalog census accepts it."""

from __future__ import annotations

import time
from datetime import date, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

from apps.identity import stepup
from apps.intake.models import Patient, PatientClinicEnrollment, PatientSession
from apps.scheduling import waitlist
from django.test import override_settings
from django.utils import timezone

from auth.stepup_test_support import STEP_UP_NOW
from identity.delegation_probes import delegation_probes
from identity.nonstaff_differential import (
    DifferentialProbe,
    DifferentialReport,
    assert_behavioral_classifications,
)
from identity.nonstaff_infrastructure_probes import (
    infrastructure_probes,
    metrics_probes,
)
from identity.nonstaff_patient_probes import patient_probes
from identity.nonstaff_principal_probes import principal_probes
from identity.nonstaff_realtime_probes import realtime_probes
from identity.nonstaff_states import CareScope, ReplayScope
from identity.nonstaff_subjects import seed_nonstaff
from identity.nonstaff_workflow_probes import workflow_probes
from identity.permission_support import owner_context

if TYPE_CHECKING:
    from collections.abc import Sequence

    import pytest

    from identity.guard_classification import Candidate
    from rbac_fixtures import RbacGraph


def run_nonstaff_census(
    candidates: Sequence[Candidate], graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> DifferentialReport:
    stamp = timezone.now()
    with (
        monkeypatch.context() as patch,
        override_settings(
            BILLING_SYNTHETIC_PIX=True,
            PRESCRIPTION_SYNTHETIC_SIGNING=True,
            PHYSICIAN_SYNTHETIC_REGISTRY=True,
            TELECONSULT_SYNTHETIC_PROVIDER=True,
            ALLOWED_HOSTS=["testserver"],
            # This owner-bootstrap scenario tests authority, not KDF cost.
            # Validation and the real Django hasher still run for every subset.
            PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
        ),
    ):
        patch.setattr(stepup, "_utc_now_seconds", lambda: STEP_UP_NOW)
        # Audit writes must use fresh occurrence times: PostgreSQL validates a
        # five-minute window. Do not freeze the audit clock over a long replay.
        patch.setattr(time, "time", stamp.timestamp)
        patch.setattr(waitlist, "OFFER_LIFETIME", timedelta(days=30))
        data = seed_nonstaff(graph, patch)
        with owner_context(graph.organization_a):
            # Fixed, long-lived synthetic protocol inputs: machine speed must
            # not turn later subsets into expired-session negative controls.
            PatientSession.objects.filter(pk=data.op.patient_session).update(
                expires_at=stamp + timedelta(days=30),
                idle_expires_at=stamp + timedelta(days=30),
            )
            patient = Patient.objects.create(
                organization_id=graph.organization_a,
                full_name="Sintetico scope",
                birth_date=date(1990, 1, 1),
            )
            other_enrollment = PatientClinicEnrollment.objects.create(
                organization_id=graph.organization_a,
                clinic_id=graph.clinic_a,
                patient=patient,
                idempotency_key=uuid4(),
                create_fingerprint=b"d" * 32,
            )
        probes: dict[str, list[DifferentialProbe]] = {}
        for probe in [
            *patient_probes(data),
            *infrastructure_probes(data, patch),
            *metrics_probes(data, patch),
            *realtime_probes(data),
            *principal_probes(graph),
            *delegation_probes(graph, data.actor.pk),
            *workflow_probes(data, patch),
        ]:
            probes.setdefault(probe.symbol, []).append(probe)
        return assert_behavioral_classifications(
            candidates,
            probes,
            actor=data.actor,
            scope=ReplayScope(
                graph.clinic_a,
                graph.organization_a,
                other_clinic=graph.clinic_b,
                care=CareScope(data.op.enrollment, other_enrollment.pk),
            ),
        )
