"""Owner-set AI switches, so the resilience suite can prove manual work with AI off."""

from __future__ import annotations

import json
import sys
from uuid import UUID

from apps.ai.contracts import PURPOSES
from apps.ai.controls import set_organization_policy
from apps.ai.models import GatewaySwitch
from apps.ai.owner import set_global_switch
from apps.ai.policy import OrganizationPolicy, organization_policy
from apps.identity.clinic_configuration import (
    ConfigurationContent,
    latest_configuration,
    publish_configuration,
)
from apps.identity.models import Clinic, UserClinicRole
from apps.tenancy.db import tenant_context

from identity.permission_support import owner_context
from patient_service_support import runtime_role

BUDGET_MICROS = 10_000_000
SWITCH = {"on": True, "off": False}


def set_switches(
    *,
    actor: UUID,
    clinic: UUID,
    organization: UUID,
    global_enabled: bool,
    organization_enabled: bool,
) -> dict[str, bool]:
    """Keep every purpose opted in, so only the two named switches decide."""
    with owner_context(organization):
        # Publishing the organization policy needs authority in every clinic.
        for target in Clinic.objects.filter(organization_id=organization):
            UserClinicRole.objects.get_or_create(
                organization_id=organization,
                clinic=target,
                user_id=actor,
                role="org_admin",
            )
    set_global_switch(enabled=global_enabled, approved_regions=("br-synthetic",))
    with runtime_role(), tenant_context(actor, organization):
        current = latest_configuration(clinic)
        if current is None:
            current = publish_configuration(
                clinic_id=clinic,
                expected_version=0,
                content=ConfigurationContent("Synthetic resilience clinic"),
            )
        set_organization_policy(
            clinic_id=clinic,
            expected_version=current.version,
            policy=OrganizationPolicy(
                enabled=organization_enabled,
                purposes=PURPOSES,
                monthly_budget_micros=BUDGET_MICROS,
                purpose_budgets=tuple((purpose, BUDGET_MICROS) for purpose in PURPOSES),
            ),
        )
        return {
            "global": GatewaySwitch.objects.filter(pk=1, enabled=True).exists(),
            "organization": organization_policy().enabled,
        }


def main() -> None:
    """The browser suite lends its owner connection; each switch is on or off."""
    actor, clinic, organization = (UUID(item) for item in sys.argv[1:4])
    state = set_switches(
        actor=actor,
        clinic=clinic,
        organization=organization,
        global_enabled=SWITCH[sys.argv[4]],
        organization_enabled=SWITCH[sys.argv[5]],
    )
    sys.stdout.write(json.dumps(state, sort_keys=True) + "\n")
