"""Owner-only write surface of the provider capability registry.

Every function asserts the ``clinic_owner`` database role and runs inside
one transaction so a denied transition rolls back the approval row it was
about to reference. Legal transitions are enforced by the
``providers_version_transition_v1`` trigger; these functions only choose
which transition to attempt. A denied attempt surfaces as
``django.db.utils.ProgrammingError`` with SQLSTATE ``P0001``
(``provider_transition_denied``); the management command maps it to a
payload-free ``CommandError``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from django.db import transaction
from django.utils import timezone

from apps.audit.models import SYSTEM_ORG_ID
from apps.audit.services import record_phase1_event
from apps.identity.management.base import (
    LifecycleCommandError,
    assert_owner_database_role,
)
from apps.providers.models import (
    ActivationRecord,
    CapabilityApproval,
    CapabilityVersion,
    HealthEvent,
    ProviderCapability,
)

if TYPE_CHECKING:
    from collections.abc import Iterable
    from uuid import UUID

KEY_PATTERN: Final = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
PROPOSED_STATES: Final = frozenset(
    {
        CapabilityVersion.State.RESEARCHED,
        CapabilityVersion.State.SELECTED_IN_PLAN,
    }
)
# States from which ``approve`` walks the ungated ``selected_in_plan`` step
# before binding the approval to ``approved_to_test``; every other state is
# attempted directly so the trigger itself denies it with P0001.
_APPROVAL_WALKS: Final[dict[str, tuple[str, ...]]] = {
    CapabilityVersion.State.RESEARCHED: (
        CapabilityVersion.State.SELECTED_IN_PLAN,
        CapabilityVersion.State.APPROVED_TO_TEST,
    ),
    CapabilityVersion.State.SELECTED_IN_PLAN: (
        CapabilityVersion.State.APPROVED_TO_TEST,
    ),
}
# States from which ``activate`` walks the remaining legal steps to
# ``activated``; every other state is attempted directly so the trigger
# itself denies it with P0001.
_ACTIVATION_WALKS: Final[dict[str, tuple[str, ...]]] = {
    CapabilityVersion.State.APPROVED_TO_TEST: (
        CapabilityVersion.State.SANDBOX,
        CapabilityVersion.State.PRODUCTION_AUTHORIZED,
        CapabilityVersion.State.ACTIVATED,
    ),
    CapabilityVersion.State.SANDBOX: (
        CapabilityVersion.State.PRODUCTION_AUTHORIZED,
        CapabilityVersion.State.ACTIVATED,
    ),
    CapabilityVersion.State.PRODUCTION_AUTHORIZED: (CapabilityVersion.State.ACTIVATED,),
    CapabilityVersion.State.DEGRADED: (CapabilityVersion.State.ACTIVATED,),
}


@dataclass(frozen=True, slots=True)
class ApprovalInput:
    """The accountable owner decision recorded with a gated transition."""

    approver_name: str
    approver_role: str
    evidence_uri: str


@dataclass(frozen=True, slots=True)
class VersionInput:
    """The provider version facts recorded by ``propose``."""

    provider: str
    account: str = ""
    environment: str = ""
    api_version: str = ""
    region: str = ""
    retention_terms: str = ""
    state: str = CapabilityVersion.State.RESEARCHED
    record_ref: str = ""
    description: str = ""


def _audit(event_type: str, capability: ProviderCapability, record_id: UUID) -> None:
    """Append the registered system-chain event for one transition."""
    record_phase1_event(
        event_type,
        clinic_id=capability.clinic_id or SYSTEM_ORG_ID,
        affected_record_id=record_id,
    )


def _locked_capability(key: str, clinic_id: UUID | None) -> ProviderCapability:
    """Return the locked capability row or reject without echoing input."""
    capability = (
        ProviderCapability.objects.select_for_update()
        .filter(key=key, clinic_id=clinic_id)
        .first()
    )
    if capability is None:
        raise LifecycleCommandError
    return capability


def _locked_current_version(capability: ProviderCapability) -> CapabilityVersion:
    """Return the locked current version row or reject."""
    version_id = capability.current_version_id
    if version_id is None:
        raise LifecycleCommandError
    version = (
        CapabilityVersion.objects.select_for_update().filter(pk=version_id).first()
    )
    if version is None:
        raise LifecycleCommandError
    return version


def _new_approval(
    capability: ProviderCapability, decision: ApprovalInput
) -> CapabilityApproval:
    """Persist one immutable owner decision."""
    return CapabilityApproval.objects.create(
        capability=capability,
        approver_name=decision.approver_name,
        approver_role=decision.approver_role,
        evidence_uri=decision.evidence_uri,
        decided_at=timezone.now(),
    )


def propose_version(
    key: str,
    *,
    clinic_id: UUID | None = None,
    version_input: VersionInput,
) -> CapabilityVersion:
    """Record a researched or selected_in_plan version as current.

    Proposing a new version replaces ``current_version`` immediately: a
    capability whose replacement is still being researched is not live,
    even when the superseded version was activated.
    """
    assert_owner_database_role()
    if (
        not KEY_PATTERN.fullmatch(key)
        or version_input.state not in PROPOSED_STATES
        or not version_input.provider
    ):
        raise LifecycleCommandError
    with transaction.atomic():
        capability = (
            ProviderCapability.objects.select_for_update()
            .filter(key=key, clinic_id=clinic_id)
            .first()
        )
        if capability is None:
            capability = ProviderCapability.objects.create(
                key=key,
                clinic_id=clinic_id,
                record_ref=version_input.record_ref,
                description=version_input.description,
            )
        version = CapabilityVersion.objects.create(
            capability=capability,
            provider=version_input.provider,
            account=version_input.account,
            environment=version_input.environment,
            api_version=version_input.api_version,
            region=version_input.region,
            retention_terms=version_input.retention_terms,
            state=version_input.state,
        )
        capability.current_version = version
        capability.save(update_fields=["current_version", "updated_at"])
        _audit("providers.capability.proposed", capability, version.id)
    return version


def approve_version(
    key: str,
    *,
    clinic_id: UUID | None = None,
    decision: ApprovalInput,
) -> CapabilityVersion:
    """Move the current version to ``approved_to_test`` under one approval.

    From ``researched`` the walk passes through the ungated
    ``selected_in_plan`` step first; the owner decision binds the gated
    ``approved_to_test`` transition. Any other state attempts
    ``approved_to_test`` directly so the trigger denies it.
    """
    assert_owner_database_role()
    with transaction.atomic():
        capability = _locked_capability(key, clinic_id)
        version = _locked_current_version(capability)
        approval = _new_approval(capability, decision)
        for target in _APPROVAL_WALKS.get(
            version.state, (CapabilityVersion.State.APPROVED_TO_TEST,)
        ):
            version.state = target
            if target == CapabilityVersion.State.APPROVED_TO_TEST:
                version.approval = approval
                version.save(update_fields=["approval", "state", "updated_at"])
            else:
                version.save(update_fields=["state", "updated_at"])
        _audit("providers.capability.approved", capability, version.id)
    return version


def activate_version(
    key: str,
    *,
    clinic_id: UUID | None = None,
    decision: ApprovalInput,
) -> CapabilityVersion:
    """Walk the current version to ``activated`` under one owner decision.

    From ``approved_to_test`` the walk passes through ``sandbox`` and
    ``production_authorized``; from ``degraded`` it re-activates directly.
    Any other state attempts ``activated`` directly so the transition
    trigger denies it with ``provider_transition_denied``.
    """
    assert_owner_database_role()
    with transaction.atomic():
        capability = _locked_capability(key, clinic_id)
        version = _locked_current_version(capability)
        approval = _new_approval(capability, decision)
        for target in _ACTIVATION_WALKS.get(
            version.state, (CapabilityVersion.State.ACTIVATED,)
        ):
            version.state = target
            version.save(update_fields=["state", "updated_at"])
        ActivationRecord.objects.create(
            capability=capability,
            version=version,
            approval=approval,
            activated_at=timezone.now(),
        )
        _audit("providers.capability.activated", capability, version.id)
    return version


def degrade_version(
    key: str,
    *,
    clinic_id: UUID | None = None,
    decision: ApprovalInput,
    reason: str,
) -> CapabilityVersion:
    """Move an ``activated`` version to ``degraded`` with a health event."""
    assert_owner_database_role()
    with transaction.atomic():
        capability = _locked_capability(key, clinic_id)
        version = _locked_current_version(capability)
        approval = _new_approval(capability, decision)
        version.state = CapabilityVersion.State.DEGRADED
        version.save(update_fields=["state", "updated_at"])
        HealthEvent.objects.create(
            capability=capability,
            version=version,
            approval=approval,
            kind=HealthEvent.Kind.DEGRADED,
            detail=reason,
            recorded_at=timezone.now(),
        )
        _audit("providers.capability.degraded", capability, version.id)
    return version


def revoke_version(
    key: str,
    *,
    clinic_id: UUID | None = None,
    decision: ApprovalInput,
    reason: str,
) -> CapabilityVersion:
    """Move any non-terminal version to the terminal ``revoked`` state."""
    assert_owner_database_role()
    with transaction.atomic():
        capability = _locked_capability(key, clinic_id)
        version = _locked_current_version(capability)
        approval = _new_approval(capability, decision)
        version.state = CapabilityVersion.State.REVOKED
        version.save(update_fields=["state", "updated_at"])
        HealthEvent.objects.create(
            capability=capability,
            version=version,
            approval=approval,
            kind=HealthEvent.Kind.REVOKED,
            detail=reason,
            recorded_at=timezone.now(),
        )
        _audit("providers.capability.revoked", capability, version.id)
    return version


def capability_rows() -> Iterable[tuple[ProviderCapability, CapabilityVersion | None]]:
    """Yield ``(capability, current_version)`` pairs ordered for reports."""
    capabilities = ProviderCapability.objects.select_related(
        "current_version"
    ).order_by("key", "clinic_id")
    return [(capability, capability.current_version) for capability in capabilities]


def report_markdown() -> str:
    """Render the lifecycle status table embedded in capabilities.md."""
    lines = [
        "| Capability | Scope | State | Provider | Environment | Region |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for capability, version in capability_rows():
        scope = "platform" if capability.clinic_id is None else "clinic override"
        provider = version.provider if version is not None else "-"
        state = version.state if version is not None else "-"
        environment = version.environment if version is not None else "-"
        region = version.region if version is not None else "-"
        lines.append(
            f"| `{capability.key}` | {scope} | {state} | {provider} "
            f"| {environment or '-'} | {region or '-'} |"
        )
    return "\n".join(lines) + "\n"


def report_text() -> str:
    """Render the same status table as aligned plain text."""
    rows = [
        (
            capability.key,
            "platform" if capability.clinic_id is None else "clinic",
            version.state if version is not None else "-",
            version.provider if version is not None else "-",
        )
        for capability, version in capability_rows()
    ]
    header = ("capability", "scope", "state", "provider")
    widths = [max(len(row[index]) for row in [*rows, header]) for index in range(4)]
    lines = [
        "  ".join(column.ljust(widths[index]) for index, column in enumerate(header))
    ]
    lines.extend(
        "  ".join(column.ljust(widths[index]) for index, column in enumerate(row))
        for row in rows
    )
    return "\n".join(lines) + "\n"
