"""Synthetic owner setup and actual runtime outbox work for the browser brief."""

from __future__ import annotations

import json
import sys
from uuid import UUID, uuid4

from apps.ai.contracts import Purpose
from apps.ai.controls import set_organization_policy
from apps.ai.models import ModelConfiguration
from apps.ai.owner import set_global_switch, transition_model
from apps.ai.policy import OrganizationPolicy
from apps.core.integration import execute_operation
from apps.identity.clinic_configuration import (
    ConfigurationContent,
    latest_configuration,
    publish_configuration,
)
from apps.identity.models import Clinic, UserClinicRole
from apps.providers.lifecycle import VersionInput, propose_version
from apps.tenancy.db import tenant_context

from ai.support import model
from identity.permission_support import owner_context
from patient_service_support import runtime_role


def seed(*, actor: UUID, clinic: UUID, organization: UUID) -> None:
    """Only the owner provisions capabilities and fixture administrator roles."""
    with owner_context(organization):
        for target in Clinic.objects.filter(organization_id=organization):
            UserClinicRole.objects.get_or_create(
                organization_id=organization,
                clinic=target,
                user_id=actor,
                role="org_admin",
            )
    propose_version(
        "llm_inference",
        version_input=VersionInput(
            provider="synthetic",
            account="synthetic-chart-" + uuid4().hex,
            environment="synthetic",
            region="synthetic",
            retention_terms="synthetic-no-retention",
        ),
    )
    set_global_switch(enabled=True, approved_regions=("br-synthetic",))
    model(organization=organization, clinic=clinic, purpose=Purpose.BRIEF)
    with runtime_role(), tenant_context(actor, organization):
        current = latest_configuration(clinic)
        if current is None:
            current = publish_configuration(
                clinic_id=clinic,
                expected_version=0,
                content=ConfigurationContent("Synthetic chart clinic"),
            )
        set_organization_policy(
            clinic_id=clinic,
            expected_version=current.version,
            policy=OrganizationPolicy(
                enabled=True,
                purposes=("brief",),
                monthly_budget_micros=10_000_000,
                purpose_budgets=(("brief", 10_000_000),),
            ),
        )


def main() -> None:
    """The browser runner lends the exact owner or clinic_app connection."""
    mode = sys.argv[1]
    if mode == "seed":
        actor, clinic, organization = (UUID(item) for item in sys.argv[2:])
        seed(actor=actor, clinic=clinic, organization=organization)
        status = "seeded"
    elif mode == "execute":
        status = execute_operation(UUID(sys.argv[2]))
    elif mode == "down":
        for active in ModelConfiguration.objects.filter(
            purpose="brief", state="active"
        ):
            transition_model(configuration_id=active.pk, state="retired")
        status = "unavailable"
    else:
        raise AssertionError(mode)
    sys.stdout.write(json.dumps({"status": status}) + "\n")
