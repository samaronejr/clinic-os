"""Human-readable effect disclosures; never emit raw source text or credentials."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final
from zoneinfo import ZoneInfo

from django.utils.translation import gettext, pgettext

from apps.ai.action_payload import closed, instant, parse_payload

if TYPE_CHECKING:
    from uuid import UUID

    from apps.ai.models import ActionProposal

STATE_LABELS: Final = {
    "proposal": "Proposed",
    "validated": "Awaiting approval",
    "approved": "Approved",
    "rejected": "Rejected",
    "executing": "Executing",
    "receipt": "Receipt recorded",
    "unknown": "Reconciliation required",
    "reconciled": "Reconciled",
    "exception": "Manual review required",
    "failure": "Failed",
    "compensating": "Compensating",
    "compensated": "Compensated",
    "manual_required": "Manual follow-up required",
}


@dataclass(frozen=True, slots=True)
class ProposalRow:
    """All displayed facts derive from the exact immutable payload being approved."""

    proposal_id: UUID
    digest: str
    digest_parts: tuple[str, ...]
    state: str
    state_label: str
    effect: str
    source_version: int
    expires_at: str
    autonomy: str
    compensation: str
    reviewable: bool
    source_revision: str
    due_at: str | None


def present_proposal(proposal: ActionProposal) -> ProposalRow:
    """Disclose one effect, no money, no real recipient, version and expiry."""
    data = parse_payload(proposal.payload).data()
    operation = closed(data["operation"], {"name", "version"})
    terms = closed(
        data["approval_policy"], {"policy_id", "version", "quorum", "autonomy"}
    )
    compensation = closed(data["compensation_policy"], {"mode", "version"})
    subject = closed(data["subject"], {"kind", "id", "expected_state", "revision"})
    zone = ZoneInfo(str(proposal.clinic.timezone))
    return ProposalRow(
        proposal.pk,
        proposal.digest,
        tuple(
            proposal.digest[index : index + 16]
            for index in range(0, len(proposal.digest), 16)
        ),
        proposal.state,
        pgettext("agent proposal state", "Reconciled")
        if proposal.state == "reconciled"
        else gettext(STATE_LABELS[proposal.state]),
        gettext("Create an operational review checklist")
        if operation["name"] == "workflows.review_task"
        else gettext("Simulate one logistics reminder"),
        proposal.run.policy.version,
        instant(data["expires_at"]).astimezone(zone).strftime("%Y-%m-%dT%H:%M"),
        str(terms["autonomy"]),
        gettext("Simulate the approved cancellation")
        if compensation["mode"] == "synthetic_cancel"
        else gettext("Require manual follow-up"),
        proposal.state == "validated",
        str(subject["revision"]),
        instant(closed(data["parameters"], {"due_at"})["due_at"])
        .astimezone(zone)
        .strftime("%Y-%m-%dT%H:%M")
        if operation["name"] == "workflows.review_task"
        else None,
    )
