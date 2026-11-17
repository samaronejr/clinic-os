"""Enable one synthetic portal assistant through the real policy and KB services."""

from __future__ import annotations

import json
import sys
from uuid import UUID, uuid4

import django


def seed(actor: UUID, clinic: UUID, organization: UUID) -> dict[str, object]:
    """Owner provisioning supplies only roles and the bounded service principal."""
    from apps.ai.agent_policies import (  # noqa: PLC0415
        PolicyTerms,
        publish_agent_policy,
    )
    from apps.ai.controls import set_organization_policy  # noqa: PLC0415
    from apps.ai.front_desk_kb import (  # noqa: PLC0415
        approve_article,
        draft_article,
    )
    from apps.ai.models import AgentPolicyVersion  # noqa: PLC0415
    from apps.ai.owner import set_global_switch  # noqa: PLC0415
    from apps.identity.clinic_configuration import (  # noqa: PLC0415
        ConfigurationContent,
        latest_configuration,
        publish_configuration,
    )
    from apps.identity.models import (  # noqa: PLC0415
        Clinic,
        ServicePrincipal,
        ServicePrincipalGrant,
        UserClinicRole,
    )
    from apps.tenancy.db import tenant_context  # noqa: PLC0415

    from ai.support import model, policy  # noqa: PLC0415
    from identity.permission_support import owner_context  # noqa: PLC0415
    from patient_service_support import runtime_role  # noqa: PLC0415

    with owner_context(organization):
        # Publishing the organization policy needs authority in every clinic,
        # including units other scenarios added to this organization.
        for target in Clinic.objects.filter(organization_id=organization):
            UserClinicRole.objects.get_or_create(
                organization_id=organization,
                clinic=target,
                user_id=actor,
                role="org_admin",
            )
        UserClinicRole.objects.filter(user_id=actor).update(workflow_bundle_version=2)
        principal = ServicePrincipal.objects.create(
            organization_id=organization,
            clinic_id=clinic,
            name=f"synthetic-portal-desk-{uuid4().hex[:8]}",
            db_identity=f"clinic_agent_front_desk_{uuid4().hex[:16]}",
            purpose="agent-execution",
            grant_set_version=2,
        )
        ServicePrincipalGrant.objects.create(
            organization_id=organization,
            principal=principal,
            permission="agent.execute",
            grant_set_version=2,
        )
    # The agent answers only while the owner's global AI switch is on.
    set_global_switch(enabled=True, approved_regions=("br-synthetic",))
    # Replies, bookings and handoffs need an active front-desk model admitted at
    # monitored_autonomy. Reach it as the unit tests do: recorded evaluation
    # runs, independent ratings and owner decisions for shadow, reviewed_live
    # and monitored_autonomy, then platform activation.
    rollout = model(organization=organization, clinic=clinic)
    with runtime_role(), tenant_context(actor, organization):
        configuration = latest_configuration(clinic)
        if configuration is None:
            configuration = publish_configuration(
                clinic_id=clinic,
                expected_version=0,
                content=ConfigurationContent("Synthetic portal assistant clinic"),
            )
        set_organization_policy(
            clinic_id=clinic, expected_version=configuration.version, policy=policy()
        )
        previous = (
            AgentPolicyVersion.objects.filter(clinic_id=clinic, capability="front_desk")
            .order_by("-version")
            .first()
        )
        publication = publish_agent_policy(
            clinic_id=clinic,
            expected_version=previous.version if previous else 0,
            terms=PolicyTerms(
                "front_desk",
                "A2",
                (
                    "comms.send_approved_link",
                    "scheduling.book_approved",
                    "scheduling.hold",
                ),
                enabled=True,
                principal_id=principal.pk,
            ),
        )
        article = draft_article(
            clinic_id=clinic,
            key="portal-hours",
            topic="hours",
            title="Horário de atendimento",
            body="Atendemos de segunda a sexta, das 8h às 18h.",
            keywords=("horario", "funcionamento"),
        )
        approved = approve_article(
            clinic_id=clinic, article_id=article.pk, expected_revision=article.revision
        )
    return {
        "synthetic": True,
        "model_configuration": str(rollout.pk),
        "policy_version": publication.version,
        "article_version": approved.version,
    }


def main() -> int:
    """Use the browser fixture's owner DSN; emit metadata only."""
    django.setup()
    actor, clinic, organization = (UUID(value) for value in sys.argv[1:])
    sys.stdout.write(
        json.dumps(seed(actor, clinic, organization), sort_keys=True) + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
