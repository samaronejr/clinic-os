"""Campaign cohorts, content, state machines, revocation and identical denial."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.audit.models import AuditEvent
from apps.comms.inbox_adapters import EmailInboxAdapter
from apps.comms.inbox_services import (
    InboxRefusedError,
    create_template_version,
    open_conversation,
    set_template_approval,
)
from apps.comms.models import IntegrationOperation, Message
from apps.consent.services import revoke_consent
from apps.core.integration import execute_operation
from apps.crm.access import CrmAccessDeniedError, CrmRuleError
from apps.crm.campaigns import (
    approve_campaign,
    campaign_audience,
    create_campaign,
    marketing_opted_in,
    refresh_campaign,
    send_campaign,
)
from apps.crm.models import Campaign, CampaignSend, MarketingPreference
from apps.crm.patient import set_marketing_preference
from apps.crm.rules import MARKETING_BODIES
from apps.intake.contacts import save_contact_destination
from apps.intake.patient_access import patient_session_context
from apps.tenancy.db import tenant_context
from django.db import DatabaseError, transaction
from django.utils import timezone

from crm.support import CrmWorld, accept_marketing, world
from identity.permission_support import owner_context
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from pytest_django.fixtures import SettingsWrapper

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def state(rbac_graph: RbacGraph, settings: SettingsWrapper) -> CrmWorld:
    return world(rbac_graph, settings)


def _campaign(state: CrmWorld) -> Campaign:
    with runtime_role(), tenant_context(state.manager, state.graph.organization_a):
        return create_campaign(
            clinic_id=state.graph.clinic_a,
            title="Synthetic neutral campaign",
            template_version_id=state.template,
        )


def _opt_in(state: CrmWorld) -> None:
    accept_marketing(state)
    with runtime_role(), patient_session_context(state.session):
        set_marketing_preference(channel="email", opted_in=True)


def test_audience_requires_both_purpose_and_patient_channel(state: CrmWorld) -> None:
    campaign = _campaign(state)
    with runtime_role(), tenant_context(state.manager, state.graph.organization_a):
        audience = campaign_audience(
            clinic_id=state.graph.clinic_a, campaign_id=campaign.pk
        )
        assert (audience.eligible, audience.excluded) == (0, 1)
    accept_marketing(state)
    with runtime_role(), tenant_context(state.manager, state.graph.organization_a):
        assert (
            campaign_audience(
                clinic_id=state.graph.clinic_a, campaign_id=campaign.pk
            ).eligible
            == 0
        )
    with runtime_role(), patient_session_context(state.session):
        choice = set_marketing_preference(channel="email", opted_in=True)
        assert set_marketing_preference(channel="email", opted_in=True).pk == choice.pk
    with runtime_role(), tenant_context(state.manager, state.graph.organization_a):
        audience = campaign_audience(
            clinic_id=state.graph.clinic_a, campaign_id=campaign.pk
        )
        assert audience.enrollments == (state.enrollment,)
        assert audience.excluded == 0


@pytest.mark.parametrize(
    "targeting",
    [{"condition": "sentinel"}, {"diagnosis": "sentinel"}, {"source": "sentinel"}],
)
def test_clinical_and_proxy_targeting_are_blocked(
    state: CrmWorld, targeting: dict[str, object]
) -> None:
    with (
        runtime_role(),
        tenant_context(state.manager, state.graph.organization_a),
        pytest.raises(CrmRuleError, match="clinical_targeting_blocked"),
    ):
        create_campaign(
            clinic_id=state.graph.clinic_a,
            title="Synthetic",
            template_version_id=state.template,
            targeting=targeting,
        )


@pytest.mark.parametrize(
    "body",
    ["Your diagnosis is {{1}}", "Return for diabetes care", "Hello {{diagnosis}}"],
)
def test_diagnosis_and_personal_placeholders_are_rejected(
    state: CrmWorld, body: str
) -> None:
    with runtime_role(), tenant_context(state.manager, state.graph.organization_a):
        try:
            template = create_template_version(
                clinic_id=state.graph.clinic_a,
                channel="email",
                name=f"unsafe_{uuid4().hex}",
                language="pt_BR",
                category="marketing",
                purpose="marketing",
                body=body,
            )
        except InboxRefusedError:
            return
        for status in ("pending", "approved"):
            set_template_approval(
                clinic_id=state.graph.clinic_a,
                template_version_id=template,
                status=status,
            )
        with pytest.raises(CrmRuleError, match="neutral_approved_template_required"):
            create_campaign(
                clinic_id=state.graph.clinic_a,
                title="Synthetic",
                template_version_id=template,
            )


def test_synthetic_campaign_and_outbox_metadata_only(state: CrmWorld) -> None:
    _opt_in(state)
    campaign = _campaign(state)
    with runtime_role(), tenant_context(state.manager, state.graph.organization_a):
        approved = approve_campaign(
            clinic_id=state.graph.clinic_a, campaign_id=campaign.pk, expected_revision=1
        )
        assert approved.state == "approved"
        with pytest.raises(CrmRuleError, match="stale_revision"):
            send_campaign(
                clinic_id=state.graph.clinic_a,
                campaign_id=campaign.pk,
                expected_revision=1,
            )
        send_campaign(
            clinic_id=state.graph.clinic_a, campaign_id=campaign.pk, expected_revision=2
        )
        sent = CampaignSend.objects.get(campaign=campaign)
        operation = IntegrationOperation.objects.get(subject_id=sent.message_id)
        assert operation.subject_type == "comms.message"
        assert not operation.last_error
        assert "diagnosis" not in str(operation)
        assert (
            refresh_campaign(
                clinic_id=state.graph.clinic_a, campaign_id=campaign.pk
            ).state
            == "sending"
        )
    with runtime_role():
        execute_operation(operation.pk)
    with runtime_role(), tenant_context(state.manager, state.graph.organization_a):
        assert (
            refresh_campaign(
                clinic_id=state.graph.clinic_a, campaign_id=campaign.pk
            ).state
            == "sent"
        )
        operation.refresh_from_db()
        assert operation.provider_reference is not None
        assert operation.provider_reference.startswith("synthetic:email:")
        assert CampaignSend.objects.filter(campaign=campaign).count() == 1
    with owner_context(state.graph.organization_a):
        assert set(
            AuditEvent.objects.filter(event_type__startswith="crm.").values_list(
                "event_type", flat=True
            )
        ) >= {
            "crm.campaign.created",
            "crm.campaign.approved",
            "crm.campaign.sending",
            "crm.send.queued",
            "crm.campaign.sent",
            "crm.preference.recorded",
        }


@pytest.mark.parametrize("revocation", ["channel", "purpose", "destination"])
def test_opt_out_after_queue_cancels_before_transport(
    state: CrmWorld, revocation: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden_send(*args: object, **kwargs: object) -> None:
        pytest.fail("revoked outreach reached the provider boundary")

    monkeypatch.setattr(EmailInboxAdapter, "send", forbidden_send)
    acceptance = accept_marketing(state)
    with runtime_role(), patient_session_context(state.session):
        set_marketing_preference(channel="email", opted_in=True)
    campaign = _campaign(state)
    with runtime_role(), tenant_context(state.manager, state.graph.organization_a):
        approve_campaign(
            clinic_id=state.graph.clinic_a, campaign_id=campaign.pk, expected_revision=1
        )
        send_campaign(
            clinic_id=state.graph.clinic_a, campaign_id=campaign.pk, expected_revision=2
        )
        operation = IntegrationOperation.objects.get(
            subject_id=CampaignSend.objects.get(campaign=campaign).message_id
        )
    if revocation == "destination":
        with (
            runtime_role(),
            tenant_context(state.graph.shared_user, state.graph.organization_a),
        ):
            save_contact_destination(
                clinic_id=state.graph.clinic_a,
                enrollment_id=state.enrollment,
                channel="email",
                destination="changed@example.invalid",
                expected_version=1,
            )
    else:
        with runtime_role(), patient_session_context(state.session):
            if revocation == "channel":
                set_marketing_preference(channel="email", opted_in=False)
            else:
                revoke_consent(acceptance_id=acceptance)
    with runtime_role():
        execute_operation(operation.pk)
    with runtime_role(), tenant_context(state.manager, state.graph.organization_a):
        operation.refresh_from_db()
        assert operation.status == "cancelled"
        # The shared outbox counts its claim before the final eligibility check.
        # forbidden_send proves that no provider call happened.
        assert operation.attempt_count == 1
        assert operation.provider_reference is None
        assert not marketing_opted_in(state.enrollment, "email")


def test_missing_consent_never_starts_campaign(state: CrmWorld) -> None:
    campaign = _campaign(state)
    with runtime_role(), tenant_context(state.manager, state.graph.organization_a):
        approve_campaign(
            clinic_id=state.graph.clinic_a, campaign_id=campaign.pk, expected_revision=1
        )
        with pytest.raises(CrmRuleError, match="no_consented_audience"):
            send_campaign(
                clinic_id=state.graph.clinic_a,
                campaign_id=campaign.pk,
                expected_revision=2,
            )
        campaign.refresh_from_db()
        assert campaign.state == "approved"
        assert not CampaignSend.objects.filter(campaign=campaign).exists()


def test_staff_cannot_attest_patient_opt_in(state: CrmWorld) -> None:
    with (
        runtime_role(),
        tenant_context(state.manager, state.graph.organization_a),
        transaction.atomic(),
        pytest.raises(DatabaseError),
    ):
        MarketingPreference.objects.create(
            organization_id=state.graph.organization_a,
            clinic_id=state.graph.clinic_a,
            enrollment_id=state.enrollment,
            patient_session_id=state.session,
            channel="email",
            opted_in=True,
            contact_version=1,
            revision=1,
        )


def test_marketing_transport_rebuilds_only_the_neutral_template(
    state: CrmWorld,
) -> None:
    """Even an independently inserted encrypted body cannot become outreach."""
    _opt_in(state)
    with runtime_role(), tenant_context(state.manager, state.graph.organization_a):
        thread = open_conversation(
            clinic_id=state.graph.clinic_a,
            enrollment_id=state.enrollment,
            channel="email",
            purpose="marketing",
        )
        message = Message.objects.create(
            organization_id=state.graph.organization_a,
            conversation_id=thread.conversation_id,
            direction="out",
            body="SYNTHETIC_PHI_MARKER_DO_NOT_SEND",
            template_id=state.template,
            template_parameters="[]",
            sent_by_id=state.manager,
            command_id=uuid4(),
            created_at=timezone.now(),
        )
        prepared = EmailInboxAdapter().prepare(
            IntegrationOperation(
                channel="email",
                subject_type="comms.message",
                subject_id=message.pk,
            )
        )
        assert prepared.body == MARKETING_BODIES[0]
        assert "SYNTHETIC_PHI_MARKER" not in prepared.body


def test_unknown_and_foreign_campaign_identical_denial(state: CrmWorld) -> None:
    campaign = _campaign(state)
    for clinic_id in (state.graph.clinic_b, state.graph.clinic_c):
        with runtime_role(), tenant_context(state.manager, state.graph.organization_a):
            for campaign_id in (campaign.pk, uuid4()):
                with pytest.raises(
                    CrmAccessDeniedError, match="current actor unauthorized"
                ):
                    campaign_audience(clinic_id=clinic_id, campaign_id=campaign_id)
