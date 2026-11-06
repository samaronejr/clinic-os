"""Owner-only synthetic setup and runtime dispatch for the care-coordination browser.

The owner provisions synthetic capabilities, roles and a finalized plan note through
the production domain commands; the model run executes as ``clinic_app`` exactly as
the worker does. Receipts carry identifiers, states and closed codes only.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

from apps.ai.agent_policies import PolicyTerms, publish_agent_policy
from apps.ai.care_models import CareProposal
from apps.ai.contracts import Purpose
from apps.ai.controls import set_organization_policy
from apps.ai.models import AgentPolicyVersion, AIInvocation
from apps.ai.owner import set_global_switch
from apps.ai.policy import OrganizationPolicy
from apps.audit.models import AuditEvent
from apps.comms.models import IntegrationOperation
from apps.core.integration import execute_operation
from apps.ehr.finalization import finalize_version
from apps.ehr.models import Encounter, SpecialtyTemplate
from apps.ehr.services import (
    SOAP_FIELDS,
    create_draft,
    publish_template,
    record_clinical_note,
)
from apps.identity.clinic_configuration import (
    ConfigurationContent,
    latest_configuration,
    publish_configuration,
)
from apps.identity.models import (
    CareTeamMembership,
    Clinic,
    ProfessionalRegistration,
    User,
    UserClinicRole,
)
from apps.identity.stepup import stamp_step_up_verification
from apps.intake.models import Patient, PatientClinicEnrollment
from apps.providers.lifecycle import VersionInput, propose_version
from apps.tenancy.db import tenant_context
from apps.workflows.care import services as care
from apps.workflows.care.models import CareAction
from apps.workflows.care.validation import CareActionSpec, return_window
from django.contrib.sessions.backends.db import SessionStore
from django.db import connection
from django.http import HttpRequest, HttpResponse
from django.utils import timezone
from django_otp import DEVICE_ID_SESSION_KEY
from django_otp.middleware import OTPMiddleware

from ai.support import model
from identity.permission_support import owner_context
from otp_test_support import get_totp_device
from patient_service_support import runtime_role

BUDGET = 10_000_000


def configure(*, organization: UUID, clinic: UUID, manager: UUID) -> None:
    """Synthetic gateway, organization opt-in and the A1 care agent policy."""
    secret = Path(os.environ["CLINIC_SECRET_DIR"]) / "agent-attestation-v1.secret"
    if not secret.exists():
        descriptor = os.open(secret, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(secrets.token_hex(32))
    administrator = User.objects.create(username=f"synthetic-care-{uuid4().hex}")
    with owner_context(organization):
        for target in Clinic.objects.filter(organization_id=organization):
            UserClinicRole.objects.create(
                organization_id=organization,
                clinic=target,
                user=administrator,
                role="org_admin",
            )
        UserClinicRole.objects.create(
            organization_id=organization,
            clinic_id=clinic,
            user=administrator,
            role="clinic_admin",
        )
    account = f"synthetic-care-{uuid4().hex}"
    for capability in ("llm_inference", "email"):
        propose_version(
            capability,
            version_input=VersionInput(
                provider="synthetic",
                account=account,
                environment="synthetic",
                api_version="v1",
                region="synthetic",
                retention_terms="synthetic-no-retention",
            ),
        )
    set_global_switch(enabled=True, approved_regions=("br-synthetic",))
    model(organization=organization, clinic=clinic, purpose=Purpose.CARE_COORDINATION)
    with runtime_role(), tenant_context(administrator.pk, organization):
        current = latest_configuration(clinic)
        if current is None:
            current = publish_configuration(
                clinic_id=clinic,
                expected_version=0,
                content=ConfigurationContent("Synthetic care clinic"),
            )
        set_organization_policy(
            clinic_id=clinic,
            expected_version=current.version,
            policy=OrganizationPolicy(
                enabled=True,
                purposes=(Purpose.CARE_COORDINATION.value,),
                monthly_budget_micros=BUDGET,
                purpose_budgets=((Purpose.CARE_COORDINATION.value, BUDGET),),
            ),
        )
        if not SpecialtyTemplate.objects.filter(clinic_id=clinic).exists():
            publish_template(
                clinic_id=clinic,
                key="geral",
                title="Clínica geral sintética",
                prompts=dict.fromkeys(SOAP_FIELDS, "Registro do médico"),
            )
    with runtime_role(), tenant_context(manager, organization):
        previous = (
            AgentPolicyVersion.objects.filter(
                clinic_id=clinic, capability="care_coordination"
            )
            .order_by("-version")
            .first()
        )
        publish_agent_policy(
            clinic_id=clinic,
            expected_version=previous.version if previous else 0,
            terms=PolicyTerms(
                "care_coordination", "A1", ("care_action.create",), enabled=True
            ),
        )


def _verified(physician: UUID) -> HttpRequest:
    """The browser physician's own confirmed device, freshly verified."""
    device = get_totp_device(physician, confirmed=True)
    request = HttpRequest()
    request.session = SessionStore()
    request.session[DEVICE_ID_SESSION_KEY] = device.persistent_id
    stamp_step_up_verification(request)
    request.user = User.objects.get(pk=physician)
    OTPMiddleware(lambda _request: HttpResponse())(request)
    return request


def plan(*, organization: UUID, clinic: UUID, physician: UUID, text: str) -> UUID:
    """One finalized note of the physician whose plan section is ``text``."""
    now = timezone.now()
    with owner_context(organization):
        patient = Patient.objects.create(
            organization_id=organization,
            full_name=f"Sintetico Coordenacao {uuid4().hex[:6]}",
            birth_date=now.date() - timedelta(days=12000),
        )
        enrollment = PatientClinicEnrollment.objects.create(
            organization_id=organization,
            clinic_id=clinic,
            patient=patient,
            idempotency_key=uuid4(),
            create_fingerprint=b"s" * 32,
        )
        CareTeamMembership.objects.create(
            organization_id=organization,
            clinic_id=clinic,
            patient_enrollment=enrollment,
            user_id=physician,
            role="physician",
            valid_from=now - timedelta(days=1),
            valid_to=now + timedelta(days=1),
        )
        if not ProfessionalRegistration.objects.filter(
            clinic_id=clinic, user_id=physician
        ).exists():
            ProfessionalRegistration.objects.create(
                organization_id=organization,
                clinic_id=clinic,
                user_id=physician,
                role="physician",
                council="CRM",
                number="SINTETICO-053-" + uuid4().hex[:8],
                jurisdiction=Clinic.objects.get(pk=clinic).crm_uf,
                specialty="Sintetico",
                status="regular",
                valid_from=now - timedelta(days=1),
                valid_to=now + timedelta(days=1),
            )
        encounter = Encounter.objects.create(
            organization_id=organization,
            clinic_id=clinic,
            patient=patient,
            physician_id=physician,
            unscheduled_reason="documentation_only",
        )
        template = SpecialtyTemplate.objects.filter(clinic_id=clinic).latest("version")
    request = _verified(physician)
    with runtime_role(), tenant_context(physician, organization):
        draft = create_draft(
            clinic_id=clinic, encounter_id=encounter.pk, template_id=template.pk
        )
        saved = record_clinical_note(
            clinic_id=clinic,
            version_id=draft.pk,
            expected_revision=draft.revision,
            content={
                **dict.fromkeys(SOAP_FIELDS, "Registro sintético"),
                "plan": text,
            },
        )
        version = finalize_version(
            clinic_id=clinic,
            version_id=draft.pk,
            expected_revision=saved.revision,
            request=request,
        )
        start, end = return_window(now=now, weeks=2)
        created = care.create_care_plan_from_version(
            clinic_id=clinic,
            version_id=version.pk,
            actions=(
                CareActionSpec(
                    kind="return_window", window_start=start, window_end=end
                ),
            ),
            idempotency_key=uuid4(),
        )
    return created.pk


def dispatch(*, organization: UUID, invocation: UUID) -> dict[str, object]:
    """Run the queued synthetic invocation through the real worker boundary."""
    with owner_context(organization):
        operation = AIInvocation.objects.get(pk=invocation).operation_id
    assert operation is not None
    with runtime_role():
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_user")
            principal = cursor.fetchone()
        assert principal is not None
        return {
            "state": execute_operation(operation_id=operation),
            "runtime_role": principal[0],
        }


def inspect(*, organization: UUID, invocation: UUID) -> dict[str, object]:
    with owner_context(organization):
        items = list(
            CareProposal.objects.filter(invocation_id=invocation).order_by("position")
        )
        actions = sorted(
            CareAction.objects.filter(
                pk__in=[item.care_action_id for item in items if item.care_action_id]
            ).values_list("kind", flat=True)
        )
    return {
        "items": [[item.kind, item.state, item.reason_code] for item in items],
        "actions": actions,
    }


def sinks(*, organization: UUID, sentinel: str) -> dict[str, object]:
    assert sentinel.startswith("SINTETICO-")
    with owner_context(organization):
        audit = list(AuditEvent.objects.values())
        outbox = list(IntegrationOperation.objects.values())
        stored = list(CareProposal.objects.values())
    assert audit
    return {
        "audit_clean": sentinel not in json.dumps(audit, default=str),
        "outbox_clean": sentinel not in json.dumps(outbox, default=str),
        "last_error_clean": all(sentinel not in row["last_error"] for row in outbox),
        "proposals_clean": sentinel not in json.dumps(stored, default=str),
    }


def main() -> None:
    """Called only after django.setup with the runner's explicit owner URL."""
    data = json.loads(sys.stdin.read())
    organization, clinic = UUID(data["organization"]), UUID(data["clinic_a"])
    action = data["action"]
    receipt: dict[str, object]
    if action == "configure":
        configure(
            organization=organization, clinic=clinic, manager=UUID(data["manager"])
        )
        receipt = {"synthetic": True}
    elif action == "plan":
        receipt = {
            "plan_id": str(
                plan(
                    organization=organization,
                    clinic=clinic,
                    physician=UUID(data["physician_a_id"]),
                    text=data["text"],
                )
            )
        }
    elif action == "dispatch":
        receipt = dispatch(
            organization=organization, invocation=UUID(data["invocation"])
        )
    elif action == "inspect":
        receipt = inspect(
            organization=organization, invocation=UUID(data["invocation"])
        )
    else:
        assert action == "sinks"
        receipt = sinks(organization=organization, sentinel=data["sentinel"])
    sys.stdout.write(json.dumps(receipt, sort_keys=True) + "\n")
