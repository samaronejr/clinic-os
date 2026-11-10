"""Human-approved campaigns through the existing comms marketing outbox."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID, uuid5

from django.db import transaction

from apps.audit.services import record_phase1_event
from apps.comms.inbox_services import open_conversation, send_message
from apps.comms.models import IntegrationOperation, TemplateVersion
from apps.consent.services import consent_for_future_use
from apps.core.integration import hold_subject_mutation_key
from apps.crm.access import CrmAccessDeniedError, CrmRuleError, Reason, manager
from apps.crm.models import Campaign, CampaignSend, MarketingPreference
from apps.crm.rules import MARKETING_BODIES, MAX_NAME
from apps.identity.models import Clinic
from apps.identity.overlay_content import validate_overlay_text
from apps.intake.models import PatientClinicEnrollment, PatientContact

if TYPE_CHECKING:
    from collections.abc import Mapping

SEND_NAMESPACE = UUID("e88008a2-57e0-4b5d-87b4-d795936b74fa")
MAX_AUDIENCE = 200


@dataclass(frozen=True, slots=True)
class Audience:
    """Recipient identifiers stay internal; the UI uses only counts."""

    enrollments: tuple[UUID, ...]
    excluded: int

    @property
    def eligible(self) -> int:
        """Report the explicit consent cohort size."""
        return len(self.enrollments)


def marketing_lock(clinic_id: UUID) -> str:
    """One stable parent orders every channel opt-out and consent revocation."""
    return f"crm.marketing:{clinic_id}"


def marketing_opted_in(enrollment_id: UUID, channel: str) -> bool:
    """Require current purpose consent AND the patient's verified channel choice."""
    enrollment = PatientClinicEnrollment.objects.filter(pk=enrollment_id).first()
    if enrollment is None or channel not in {"email", "sms", "whatsapp"}:
        return False
    preference = (
        MarketingPreference.objects.filter(enrollment=enrollment, channel=channel)
        .order_by("-revision")
        .first()
    )
    contact = PatientContact.objects.filter(
        patient_id=enrollment.patient_id, channel=channel
    ).first()
    return bool(
        preference
        and preference.opted_in
        and contact
        and contact.destination_version == contact.verified_version
        and contact.verified_version == preference.contact_version
        and consent_for_future_use(
            clinic_id=enrollment.clinic_id,
            enrollment_id=enrollment_id,
            purpose="marketing",
        )
    )


def _template(clinic_id: UUID, template_id: UUID) -> TemplateVersion:
    template = TemplateVersion.objects.filter(
        pk=template_id, clinic_id=clinic_id
    ).first()
    if template is None:
        raise CrmAccessDeniedError
    if (
        template.purpose != "marketing"
        or template.category != "marketing"
        or template.parameter_count != 0
        or template.body not in MARKETING_BODIES
        or template.approval_status != "approved"
    ):
        raise CrmRuleError(Reason.NEUTRAL_TEMPLATE)
    return template


def create_campaign(
    *,
    clinic_id: UUID,
    title: str,
    template_version_id: UUID,
    targeting: Mapping[str, object] | None = None,
) -> Campaign:
    """Default-deny clinical targeting; no reviewed lawful-basis record exists."""
    actor = manager(clinic_id)
    if targeting:
        raise CrmRuleError(Reason.CLINICAL_TARGETING)
    if not isinstance(title, str) or not 0 < len(title.strip()) <= MAX_NAME:
        raise CrmRuleError(Reason.INVALID_CAMPAIGN)
    validate_overlay_text(title)
    template = _template(clinic_id, template_version_id)
    with transaction.atomic():
        campaign = Campaign.objects.create(
            organization_id=Clinic.objects.get(pk=clinic_id).organization_id,
            clinic_id=clinic_id,
            title=title.strip(),
            template=template,
            created_by_id=actor,
        )
        record_phase1_event(
            "crm.campaign.created", clinic_id=clinic_id, affected_record_id=campaign.pk
        )
    return campaign


def _campaign(
    clinic_id: UUID, campaign_id: UUID, revision: int | None = None
) -> Campaign:
    manager(clinic_id)
    campaign = (
        Campaign.objects.select_for_update()
        .filter(pk=campaign_id, clinic_id=clinic_id)
        .first()
    )
    if campaign is None:
        raise CrmAccessDeniedError
    if revision is not None and campaign.revision != revision:
        raise CrmRuleError(Reason.STALE)
    return campaign


def campaign_audience(*, clinic_id: UUID, campaign_id: UUID) -> Audience:
    """Only consent dimensions are queried; no clinical table is read."""
    manager(clinic_id)
    campaign = Campaign.objects.filter(pk=campaign_id, clinic_id=clinic_id).first()
    if campaign is None:
        raise CrmAccessDeniedError
    template = _template(clinic_id, campaign.template_id)
    cohort = tuple(
        PatientClinicEnrollment.objects.filter(clinic_id=clinic_id)
        .order_by("pk")
        .values_list("pk", flat=True)[: MAX_AUDIENCE + 1]
    )
    if len(cohort) > MAX_AUDIENCE:
        raise CrmRuleError(Reason.AUDIENCE_LIMIT)
    eligible = tuple(pk for pk in cohort if marketing_opted_in(pk, template.channel))
    return Audience(eligible, len(cohort) - len(eligible))


def approve_campaign(
    *, clinic_id: UUID, campaign_id: UUID, expected_revision: int
) -> Campaign:
    """Freeze the already immutable neutral template under a human command."""
    with transaction.atomic():
        campaign = _campaign(clinic_id, campaign_id, expected_revision)
        _template(clinic_id, campaign.template_id)
        if campaign.state != Campaign.State.DRAFT:
            raise CrmRuleError(Reason.ILLEGAL_TRANSITION)
        campaign.state = Campaign.State.APPROVED
        campaign.revision += 1
        campaign.save(update_fields=["state", "revision"])
        record_phase1_event(
            "crm.campaign.approved", clinic_id=clinic_id, affected_record_id=campaign.pk
        )
        return campaign


def send_campaign(
    *, clinic_id: UUID, campaign_id: UUID, expected_revision: int
) -> Campaign:
    """Queue a consent-filtered synthetic cohort; no transport runs in this call."""
    with transaction.atomic():
        hold_subject_mutation_key(marketing_lock(clinic_id))
        campaign = _campaign(clinic_id, campaign_id, expected_revision)
        if campaign.state != Campaign.State.APPROVED:
            raise CrmRuleError(Reason.ILLEGAL_TRANSITION)
        audience = campaign_audience(clinic_id=clinic_id, campaign_id=campaign_id)
        if not audience.enrollments:
            raise CrmRuleError(Reason.NO_AUDIENCE)
        campaign.state = Campaign.State.SENDING
        campaign.excluded_count = audience.excluded
        campaign.revision += 1
        campaign.save(update_fields=["state", "excluded_count", "revision"])
        record_phase1_event(
            "crm.campaign.sending", clinic_id=clinic_id, affected_record_id=campaign.pk
        )
        template = _template(clinic_id, campaign.template_id)
        for enrollment_id in audience.enrollments:
            opened = open_conversation(
                clinic_id=clinic_id,
                enrollment_id=enrollment_id,
                channel=template.channel,
                purpose="marketing",
            )
            receipt = send_message(
                clinic_id=clinic_id,
                conversation_id=opened.conversation_id,
                command_id=uuid5(SEND_NAMESPACE, f"{campaign_id}:{enrollment_id}"),
                template_version_id=template.pk,
            )
            sent = CampaignSend.objects.create(
                organization_id=campaign.organization_id,
                clinic_id=clinic_id,
                campaign=campaign,
                enrollment_id=enrollment_id,
                message_id=receipt.message_id,
            )
            record_phase1_event(
                "crm.send.queued", clinic_id=clinic_id, affected_record_id=sent.pk
            )
        return campaign


def refresh_campaign(*, clinic_id: UUID, campaign_id: UUID) -> Campaign:
    """Mark sent only with successful outbox receipts; never claim delivery."""
    with transaction.atomic():
        campaign = _campaign(clinic_id, campaign_id)
        if campaign.state != Campaign.State.SENDING:
            return campaign
        messages = CampaignSend.objects.filter(campaign=campaign).values_list(
            "message_id", flat=True
        )
        operations = IntegrationOperation.objects.filter(
            subject_type="comms.message", subject_id__in=messages
        )
        if (
            operations.exists()
            and not operations.exclude(status__in=("succeeded", "delivered")).exists()
        ):
            campaign.state = Campaign.State.SENT
            campaign.revision += 1
            campaign.save(update_fields=["state", "revision"])
            record_phase1_event(
                "crm.campaign.sent", clinic_id=clinic_id, affected_record_id=campaign.pk
            )
        return campaign
