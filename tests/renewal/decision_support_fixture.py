"""Disposable owner gate setup and runtime work for decision-support browser QA."""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from datetime import timedelta
from typing import TYPE_CHECKING
from unittest.mock import patch
from uuid import UUID, uuid4

from apps.ai.adapters import SyntheticProvider
from apps.ai.contracts import ProviderResult, Purpose
from apps.ai.controls import set_organization_policy
from apps.ai.decision_policy import GATE_CAPABILITIES
from apps.ai.policy import organization_policy
from apps.consent.services import record_ai_disclosure
from apps.core.integration import execute_operation
from apps.ehr.models import Encounter
from apps.identity.clinic_configuration import latest_configuration
from apps.identity.models import (
    CareTeamMembership,
    Clinic,
    ProfessionalRegistration,
    User,
    UserClinicRole,
)
from apps.intake.models import PatientClinicEnrollment
from apps.providers.lifecycle import (
    ApprovalInput,
    VersionInput,
    approve_version,
    propose_version,
    revoke_version,
)
from apps.tenancy.db import tenant_context
from django.db import connection, transaction
from django.utils import timezone

from ai.support import model
from identity.permission_support import owner_context
from patient_service_support import runtime_role
from renewal.chart_brief_fixture import seed as seed_gateway

if TYPE_CHECKING:
    from apps.ai.contracts import ProviderRequest


def disclosure_clinician(
    *, clinic: UUID, organization: UUID, encounter: UUID, physician: UUID
) -> UUID:
    """Owner-provision real registration and patient scope, not permission bypasses."""
    clinician = User.objects.create(username="synthetic-cds-disclosure-" + uuid4().hex)
    now = timezone.now()
    with owner_context(organization):
        visit = Encounter.objects.get(
            pk=encounter, clinic_id=clinic, physician_id=physician
        )
        enrollment = PatientClinicEnrollment.objects.get(
            clinic_id=clinic, patient_id=visit.patient_id
        )
        jurisdiction = Clinic.objects.get(pk=clinic).crm_uf
        UserClinicRole.objects.create(
            organization_id=organization,
            clinic_id=clinic,
            user=clinician,
            role="physician",
        )
        for user in (clinician.pk, physician):
            ProfessionalRegistration.objects.create(
                organization_id=organization,
                clinic_id=clinic,
                user_id=user,
                role="physician",
                council="CRM",
                number="SYNTHETIC-CDS-" + uuid4().hex,
                jurisdiction=jurisdiction,
                specialty="Synthetic browser fixture",
                status="regular",
                valid_from=now - timedelta(days=1),
                valid_to=now + timedelta(days=1),
            )
        CareTeamMembership.objects.create(
            organization_id=organization,
            clinic_id=clinic,
            patient_enrollment=enrollment,
            user=clinician,
            role="physician",
            valid_from=now - timedelta(days=1),
            valid_to=now + timedelta(days=1),
        )
    return clinician.pk


def seed(
    *, actor: UUID, physician: UUID, clinic: UUID, organization: UUID, encounter: UUID
) -> None:
    """Owner-only simulated records never approve a genuine clinical capability."""
    seed_gateway(actor=actor, clinic=clinic, organization=organization)
    model(purpose=Purpose.DECISION_SUPPORT)
    for key, record in GATE_CAPABILITIES:
        propose_version(
            key,
            clinic_id=clinic,
            version_input=VersionInput(
                provider="synthetic",
                account="synthetic-" + uuid4().hex,
                environment="synthetic",
                region="synthetic",
                retention_terms="synthetic-no-retention",
                record_ref=record,
            ),
        )
        version = approve_version(
            key,
            clinic_id=clinic,
            decision=ApprovalInput(
                "Synthetic browser fixture",
                "Synthetic owner; not external approval",
                f"https://synthetic.invalid/{record}",
            ),
        )
        with transaction.atomic():
            for state in ("sandbox", "production_authorized"):
                version.state = state
                version.save(update_fields=("state", "updated_at"))
    with runtime_role(), tenant_context(actor, organization):
        current = latest_configuration(clinic)
        assert current is not None
        previous = organization_policy()
        set_organization_policy(
            clinic_id=clinic,
            expected_version=current.version,
            policy=replace(
                previous,
                purposes=("brief", "decision_support"),
                purpose_budgets=(
                    ("brief", 10_000_000),
                    ("decision_support", 10_000_000),
                ),
            ),
        )
    attestor = disclosure_clinician(
        clinic=clinic,
        organization=organization,
        encounter=encounter,
        physician=physician,
    )
    with runtime_role(), tenant_context(attestor, organization):
        disclosure = record_ai_disclosure(
            clinic_id=clinic, encounter_id=encounter, informed=True, refused=False
        )
        assert disclosure.recorded_by_id == attestor


def main() -> None:
    """Use the runner's fixture connection, assuming clinic_app before any work."""
    mode = sys.argv[1]
    if mode == "seed":
        actor, physician, clinic, organization, encounter = (
            UUID(item) for item in sys.argv[2:]
        )
        seed(
            actor=actor,
            physician=physician,
            clinic=clinic,
            organization=organization,
            encounter=encounter,
        )
        status = "seeded"
    elif mode in {"execute", "fabricate"}:
        original = SyntheticProvider.complete

        def fabricated(
            self: SyntheticProvider, *, request: ProviderRequest
        ) -> ProviderResult:
            result = original(self, request=request)
            data = json.loads(result.content)
            data["model_hypotheses"][0]["reference"]["key"] = "fabricated-citation"
            return ProviderResult(json.dumps(data), result.usage)

        with runtime_role():
            with connection.cursor() as cursor:
                cursor.execute("SELECT current_user")
                assert cursor.fetchone() == ("clinic_app",)
            if mode == "fabricate":
                with patch("apps.ai.adapters.SyntheticProvider.complete", fabricated):
                    status = execute_operation(UUID(sys.argv[2]))
            else:
                status = execute_operation(UUID(sys.argv[2]))
    elif mode == "catalog-down":
        revoke_version(
            GATE_CAPABILITIES[2][0],
            clinic_id=UUID(sys.argv[2]),
            decision=ApprovalInput(
                "Synthetic fixture",
                "Synthetic owner",
                "https://synthetic.invalid/outage",
            ),
            reason="Synthetic reference catalog unavailable",
        )
        status = "unavailable"
    else:
        raise AssertionError(mode)
    sys.stdout.write(json.dumps({"status": status, "synthetic": True}) + "\n")
