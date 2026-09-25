"""Synthetic professional verification through real clinic_app authority and RLS."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.ehr.services import open_encounter
from apps.identity import physician_verification as verification
from apps.identity import stepup
from apps.identity.models import PhysicianEvidence, PhysicianProfile, UserClinicRole
from apps.identity.physician_registry import (
    RegistrationIdentity,
    RegistryResponse,
    RegistryUnavailableError,
    SigningIdentity,
    SyntheticPhysicianRegistry,
    get_physician_registry,
    registry_capability,
)
from apps.identity.physician_verification import (
    PhysicianVerificationRequired,
    verify_physician_for_signing,
)
from apps.identity.stepup import STEP_UP_SESSION_KEY, StepUpRequired
from apps.tenancy.db import tenant_context
from django.db import DatabaseError, connection, transaction
from django.test import override_settings

from auth.stepup_test_support import verified_request
from patient_service_support import runtime_role
from provider_gate_support import assert_capability_gate_closed
from scheduling.appointment_service_support import (
    create_synthetic_appointment,
    seed_appointment_setup,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from uuid import UUID

    from django.http import HttpRequest
    from pytest_django.fixtures import SettingsWrapper

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)
NOW = datetime(2036, 1, 1, tzinfo=UTC)


@contextmanager
def owner_scope(organization_id: UUID) -> Iterator[None]:
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.current_tenant', %s, true)", [str(organization_id)]
        )
        yield


@dataclass
class SigningSetup:
    graph: RbacGraph
    encounter_id: UUID
    profile: PhysicianProfile
    request: HttpRequest
    signer: SigningIdentity

    def verify(self, *, synthetic: bool = True) -> PhysicianEvidence:
        return verify_physician_for_signing(
            request=self.request,
            clinic_id=self.graph.clinic_a,
            encounter_id=self.encounter_id,
            signer=self.signer,
            synthetic=synthetic,
        )


@pytest.fixture
def setup(
    rbac_graph: RbacGraph, monkeypatch: pytest.MonkeyPatch
) -> Iterator[SigningSetup]:
    monkeypatch.setattr(verification, "utc_now", lambda: NOW)
    monkeypatch.setattr("apps.identity.physician_registry.utc_now", lambda: NOW)
    monkeypatch.setattr(stepup, "_utc_now_seconds", lambda: int(NOW.timestamp()))
    appointment_setup = seed_appointment_setup(rbac_graph)
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        appointment = create_synthetic_appointment(appointment_setup)
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        encounter = open_encounter(
            clinic_id=rbac_graph.clinic_a, appointment_id=appointment.pk
        )
    with owner_scope(rbac_graph.organization_a):
        profile = PhysicianProfile.objects.create(
            organization_id=rbac_graph.organization_a,
            user_id=rbac_graph.physician,
            jurisdiction="SP",
            registration_number="SYNTHETIC-CRM-31",
            signing_subject=f"synthetic:physician:{rbac_graph.physician}",
        )
    request = verified_request(rbac_graph.physician, verified_at=int(NOW.timestamp()))
    signer = SigningIdentity(
        rbac_graph.physician, profile.signing_subject, synthetic=True
    )
    with override_settings(PHYSICIAN_SYNTHETIC_REGISTRY=True):
        yield SigningSetup(rbac_graph, encounter.pk, profile, request, signer)


class ControlledRegistry(SyntheticPhysicianRegistry):
    def __init__(self, case: str = "regular") -> None:
        self.case = case
        self.calls = 0

    def lookup(self, identity: RegistrationIdentity) -> RegistryResponse:
        self.calls += 1
        if self.case == "outage":
            raise RegistryUnavailableError
        response = super().lookup(identity)
        responses = {
            "regular": response,
            "expired": replace(response, expires_at=NOW),
            "revoked": replace(response, status="revoked"),
            "suspended": replace(response, status="suspended"),
            "unknown": replace(response, status="unknown"),
            "unavailable": replace(response, status="unavailable"),
            "unmapped": replace(response, status="not-a-mapped-provider-state"),
            "jurisdiction": replace(
                response, identity=replace(identity, jurisdiction="RJ")
            ),
            "number": replace(
                response,
                identity=replace(identity, registration_number="SYNTHETIC-OTHER"),
            ),
            "subject": replace(
                response, identity=replace(identity, signing_subject="synthetic:other")
            ),
            "stale": replace(
                response, checked_at=NOW - timedelta(minutes=5), recheck_at=NOW
            ),
            "future": replace(response, checked_at=NOW + timedelta(seconds=1)),
            "policy": replace(response, recheck_at=NOW + timedelta(days=1)),
            "naive": replace(response, checked_at=NOW.replace(tzinfo=None)),
            "real": replace(response, synthetic=False),
            "reference": replace(response, reference=""),
        }
        return responses[self.case]


def test_regular_is_timestamped_synthetic_and_never_a_cached_grant(
    setup: SigningSetup,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ControlledRegistry()
    monkeypatch.setattr(verification, "get_physician_registry", lambda: registry)
    graph = setup.graph
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        first = setup.verify()
        second = setup.verify()
        assert first.pk != second.pk
        assert registry.calls == 2
        assert first.status == "regular"
        assert first.synthetic is True
        assert first.reason_code == "verified_synthetic"
        assert first.checked_at == NOW
        assert first.recheck_at == NOW + timedelta(minutes=5)
        assert first.reference == "SYNTHETIC-NOT-A-REGISTRY-RECEIPT"
        assert first.signing_subject == setup.signer.subject
        setup.profile.refresh_from_db()
        assert setup.profile.status == "regular"
        assert setup.profile.last_checked_at == NOW
        assert setup.profile.recheck_at == first.recheck_at


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("expired", "registration_expired"),
        ("revoked", "registration_revoked"),
        ("suspended", "registration_suspended"),
        ("unknown", "registration_unknown"),
        ("unavailable", "registration_unavailable"),
        ("unmapped", "registration_unknown"),
        ("jurisdiction", "registration_jurisdiction_mismatch"),
        ("number", "registration_number_mismatch"),
        ("subject", "registry_identity_mismatch"),
        ("stale", "registration_evidence_stale"),
        ("future", "registration_evidence_stale"),
        ("policy", "registration_evidence_stale"),
        ("naive", "registry_response_invalid"),
        ("real", "registry_response_invalid"),
        ("reference", "registry_response_invalid"),
        ("outage", "registry_unavailable"),
    ],
)
def test_failed_refresh_never_falls_back_to_prior_regular_evidence(
    setup: SigningSetup,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    reason: str,
) -> None:
    registry = ControlledRegistry()
    monkeypatch.setattr(verification, "get_physician_registry", lambda: registry)
    graph = setup.graph
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        first = setup.verify()
        registry.case = case
        with pytest.raises(PhysicianVerificationRequired) as caught:
            setup.verify()
        assert caught.value.reason_code == reason
        assert registry.calls == 2
        assert PhysicianEvidence.objects.count() == 2
        first.refresh_from_db()
        assert first.reason_code == "verified_synthetic"
        assert (
            PhysicianEvidence.objects.exclude(pk=first.pk).get().reason_code == reason
        )
        setup.profile.refresh_from_db()
        assert setup.profile.status != "regular"
        registry.case = "regular"
        recovered = setup.verify()
        assert recovered.reason_code == "verified_synthetic"
        assert registry.calls == 3


@pytest.mark.parametrize(
    "case", ["issuer", "subject", "real_signer", "real_profile", "jurisdiction"]
)
def test_signer_and_profile_binding_fail_closed(setup: SigningSetup, case: str) -> None:
    graph = setup.graph
    expected = "signing_identity_mismatch"
    if case == "issuer":
        setup.signer = replace(setup.signer, issuer_id=graph.shared_user)
    elif case == "subject":
        setup.signer = replace(setup.signer, subject="synthetic:other")
    elif case == "real_signer":
        setup.signer = replace(setup.signer, synthetic=False)
        expected = "synthetic_identity_required"
    else:
        with owner_scope(graph.organization_a):
            if case == "real_profile":
                PhysicianProfile.objects.filter(pk=setup.profile.pk).update(
                    synthetic=False
                )
                expected = "synthetic_identity_required"
            else:
                PhysicianProfile.objects.filter(pk=setup.profile.pk).update(
                    jurisdiction="RJ"
                )
                expected = "registration_profile_required"
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        with pytest.raises(PhysicianVerificationRequired, match=expected):
            setup.verify()
        assert not PhysicianEvidence.objects.exists()


def test_real_gate_and_synthetic_opt_in_cannot_be_bypassed(
    setup: SigningSetup, settings: SettingsWrapper
) -> None:
    graph = setup.graph
    assert_capability_gate_closed(
        settings, "physician_registration", probe=registry_capability
    )
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        assert registry_capability().real_enabled is False
        with pytest.raises(
            PhysicianVerificationRequired,
            match="missing_registry_provider_and_owner_approval",
        ):
            setup.verify(synthetic=False)
        with override_settings(PHYSICIAN_SYNTHETIC_REGISTRY=False):
            with pytest.raises(
                PhysicianVerificationRequired,
                match="missing_registry_provider_and_owner_approval",
            ):
                setup.verify()
            with pytest.raises(RegistryUnavailableError):
                get_physician_registry()
        assert not PhysicianEvidence.objects.exists()


def test_step_up_is_required_on_every_attempt(setup: SigningSetup) -> None:
    graph = setup.graph
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        setup.verify()
        setup.request.session[STEP_UP_SESSION_KEY] = int(NOW.timestamp()) - 301
        with pytest.raises(StepUpRequired):
            setup.verify()
        assert PhysicianEvidence.objects.count() == 1


def test_canonical_role_removal_overrides_regular_registration(
    setup: SigningSetup,
) -> None:
    graph = setup.graph
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        setup.verify()
    with owner_scope(graph.organization_a):
        UserClinicRole.objects.filter(
            user_id=graph.physician, role="physician"
        ).delete()
        UserClinicRole.objects.create(
            user_id=graph.physician,
            clinic_id=graph.clinic_a,
            organization_id=graph.organization_a,
            role="owner",
        )
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        with pytest.raises(
            PhysicianVerificationRequired, match="clinical_authority_required"
        ):
            setup.verify()
        assert not PhysicianProfile.objects.exists()
        assert not PhysicianEvidence.objects.exists()


def test_authenticated_user_must_equal_tenant_actor(setup: SigningSetup) -> None:
    graph = setup.graph
    setup.request = verified_request(
        graph.shared_user, verified_at=int(NOW.timestamp())
    )
    with (
        runtime_role(),
        tenant_context(graph.physician, graph.organization_a),
        pytest.raises(
            PhysicianVerificationRequired, match="authenticated_issuer_mismatch"
        ),
    ):
        setup.verify()


def test_another_physician_is_not_the_assigned_issuer(setup: SigningSetup) -> None:
    graph = setup.graph
    with owner_scope(graph.organization_a):
        UserClinicRole.objects.create(
            user_id=graph.shared_user,
            clinic_id=graph.clinic_a,
            organization_id=graph.organization_a,
            role="physician",
        )
    setup.request = verified_request(
        graph.shared_user, verified_at=int(NOW.timestamp())
    )
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_a):
        with pytest.raises(
            PhysicianVerificationRequired, match="assigned_physician_required"
        ):
            setup.verify()
        assert not PhysicianProfile.objects.exists()
        assert not PhysicianEvidence.objects.exists()


def test_missing_and_cross_clinic_encounters_are_non_enumerating(
    setup: SigningSetup,
) -> None:
    graph = setup.graph
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        setup.encounter_id = uuid4()
        with pytest.raises(
            PhysicianVerificationRequired, match="assigned_physician_required"
        ):
            setup.verify()
        with pytest.raises(
            PhysicianVerificationRequired, match="clinical_authority_required"
        ):
            verify_physician_for_signing(
                request=setup.request,
                clinic_id=graph.clinic_b,
                encounter_id=setup.encounter_id,
                signer=setup.signer,
                synthetic=True,
            )


def test_no_tenant_context_or_other_tenant_can_read_profiles(
    setup: SigningSetup,
) -> None:
    graph = setup.graph
    with runtime_role():
        assert not PhysicianProfile.objects.exists()
        assert not PhysicianEvidence.objects.exists()
        with pytest.raises(
            PhysicianVerificationRequired, match="clinical_authority_required"
        ):
            setup.verify()
        with tenant_context(graph.shared_user, graph.organization_b):
            assert not PhysicianProfile.objects.exists()
            assert not PhysicianEvidence.objects.exists()


def test_runtime_cannot_rebind_profiles_or_rewrite_evidence(
    setup: SigningSetup,
) -> None:
    graph = setup.graph
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        evidence = setup.verify()
        with pytest.raises(DatabaseError), transaction.atomic():
            PhysicianProfile.objects.filter(pk=setup.profile.pk).update(
                signing_subject="synthetic:forged"
            )
        with pytest.raises(DatabaseError), transaction.atomic():
            PhysicianProfile.objects.create(
                organization_id=graph.organization_a,
                user_id=graph.physician,
                jurisdiction="RJ",
                registration_number="SYNTHETIC-FORGED",
                signing_subject="synthetic:forged",
            )
        with pytest.raises(DatabaseError), transaction.atomic():
            PhysicianEvidence.objects.filter(pk=evidence.pk).update(
                reason_code="forged"
            )
        with pytest.raises(DatabaseError), transaction.atomic():
            PhysicianEvidence.objects.filter(pk=evidence.pk).delete()
        evidence.refresh_from_db()
        assert evidence.reason_code == "verified_synthetic"


def test_exact_force_rls_policies(setup: SigningSetup) -> None:
    tables = ["identity_physicianprofile", "identity_physicianevidence"]
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT relname, relrowsecurity, relforcerowsecurity "
            "FROM pg_class WHERE relname = ANY(%s)",
            [tables],
        )
        assert set(cursor.fetchall()) == {(table, True, True) for table in tables}
        cursor.execute(
            "SELECT tablename, policyname FROM pg_policies "
            "WHERE schemaname='clinic_app' AND tablename = ANY(%s)",
            [tables],
        )
        assert set(cursor.fetchall()) == {
            ("identity_physicianprofile", "physician_self"),
            ("identity_physicianprofile", "physician_provision"),
            ("identity_physicianevidence", "physician_evidence_assigned"),
            ("identity_physicianevidence", "physician_evidence_owner"),
        }


def test_synthetic_adapter_rejects_unlabelled_identity() -> None:
    with pytest.raises(RegistryUnavailableError):
        SyntheticPhysicianRegistry().lookup(
            RegistrationIdentity("SP", "123456", "unapproved")
        )
