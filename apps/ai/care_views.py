"""Care-coordination review: clinician requests and decisions, staff bundles.

Every selector travels in the POST body. The page is private and no-store;
refusals roll back inside the command boundary. Staff see only administrative
items; clinical items and validator refusals belong to the patient's clinician.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final
from uuid import UUID
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_noop
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods

from apps.ai.action_contracts import (
    ActionAccessDeniedError,
    ActionInputError,
    ActionRefusedError,
)
from apps.ai.attestations import step_up_evidence
from apps.ai.care_coordination import (
    CareAccessDeniedError,
    CareBundle,
    CareConflictError,
    ItemOutcome,
    approve_care_bundle,
    bundle_digest,
    care_bundle,
    collect_care_coordination,
    decide_care_item,
    pending_bundle,
    request_care_coordination,
)
from apps.ai.care_models import CareProposal
from apps.ai.contracts import GatewayAccessDeniedError
from apps.core.secrets import SecretUnavailableError
from apps.identity.current_context import (
    CurrentActorError,
    current_actor_id,
    require_permission,
)
from apps.identity.models import Clinic
from apps.identity.otp import flow_redirect, privileged_totp_required
from apps.identity.stepup import StepUpRequired, assert_step_up
from apps.workflows.care.models import CarePlan

if TYPE_CHECKING:
    from datetime import datetime

    from django.http import HttpRequest, HttpResponse, HttpResponseBase

KIND_LABELS: Final = {
    "return_window": gettext_noop("Return window"),
    "staff_coordination": gettext_noop("Staff coordination"),
    "order": gettext_noop("Order follow-up"),
    "questionnaire": gettext_noop("Questionnaire"),
    "monitoring_task": gettext_noop("Monitoring task"),
    "referral": gettext_noop("Referral"),
    "patient_instruction": gettext_noop("Patient instruction"),
    "unsupported": gettext_noop("Action type not allowed"),
}
STATE_LABELS: Final = {
    "pending": gettext_noop("Awaiting decision"),
    "refused": gettext_noop("Refused by the validator"),
    "approved": gettext_noop("Approved by staff"),
    "accepted": gettext_noop("Accepted by the clinician"),
    "rejected": gettext_noop("Declined"),
    "stale": gettext_noop("Plan changed; not applied"),
}
REASON_LABELS: Final = {
    "kind_forbidden": gettext_noop(
        "The assistant may not propose this kind of action. Only the clinician "
        "decides medication, procedures, referrals and patient guidance."
    ),
    "window_invalid": gettext_noop("The proposed window is outside the allowed range."),
    "malformed": gettext_noop("The suggestion was incomplete and was discarded."),
    "duplicate": gettext_noop("The same suggestion appeared twice."),
    "source_superseded": gettext_noop(
        "The plan changed after the suggestion. Request new suggestions."
    ),
}
REFUSAL_LABEL: Final = gettext_noop(
    "This item could not be approved now. Review it again or continue manually."
)
_FIELDS: Final = frozenset(
    {
        "csrfmiddlewaretoken",
        "action",
        "plan_id",
        "invocation_id",
        "item_id",
        "decision",
        "revision",
        "digest",
    }
)


def _url(clinic_id: UUID) -> str:
    return reverse("ai:care-coordination", args=(clinic_id,))


@dataclass(frozen=True, slots=True)
class ItemRow:
    """One displayed proposal: closed labels, clinic-local window, no model text."""

    item_id: UUID
    revision: int
    kind: str
    kind_label: str
    audience: str
    state: str
    state_label: str
    reason_label: str
    window_start: str
    window_end: str
    decidable: bool


@dataclass(frozen=True, slots=True)
class PlanRow:
    """One active plan its clinician may send to the assistant."""

    plan_id: UUID
    created_at: str
    patient: str
    bundles: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class BundleRow:
    """One run with administrative items still awaiting staff approval."""

    invocation_id: UUID
    created_at: str
    pending: int


def _zone(clinic_id: UUID) -> ZoneInfo:
    return ZoneInfo(str(Clinic.objects.get(pk=clinic_id).timezone))


def _minute(value: datetime | None, zone: ZoneInfo) -> str:
    """Return the clinic-local ISO minute ``clinic_minute`` displays, or ''."""
    return value.astimezone(zone).strftime("%Y-%m-%dT%H:%M") if value else ""


def _care_staff(clinic_id: UUID) -> bool:
    with connection.cursor() as cursor:
        cursor.execute("SELECT clinic_app.ai_care_staff(%s)", [clinic_id])
        row = cursor.fetchone()
    return row is not None and row[0] is True


def _clinician(clinic_id: UUID) -> bool:
    try:
        require_permission("automation.propose_clinical", clinic_id=clinic_id)
    except CurrentActorError:
        return False
    return True


def _rows(clinic_id: UUID, items: tuple[CareProposal, ...]) -> tuple[ItemRow, ...]:
    zone = _zone(clinic_id)
    return tuple(
        ItemRow(
            item.pk,
            item.revision,
            item.kind,
            _(KIND_LABELS[item.kind]),
            item.audience,
            item.state,
            _(STATE_LABELS[item.state]),
            _(REASON_LABELS[item.reason_code]) if item.reason_code else "",
            _minute(item.window_start, zone),
            _minute(item.window_end, zone),
            # Row security already limits staff to administrative items.
            item.state == "pending",
        )
        for item in items
    )


def _plans(clinic_id: UUID, zone: ZoneInfo) -> tuple[PlanRow, ...]:
    plans = (
        CarePlan.objects.filter(
            clinic_id=clinic_id, clinician_id=current_actor_id(), state="active"
        )
        .select_related("enrollment__patient")
        .order_by("-created_at")[:20]
    )
    rows = []
    for plan in plans:
        bundles = tuple(
            dict.fromkeys(
                CareProposal.objects.filter(clinic_id=clinic_id, plan=plan)
                .order_by("-created_at")
                .values_list("invocation_id", flat=True)
            )
        )
        rows.append(
            PlanRow(
                plan.pk,
                _minute(plan.created_at, zone),
                str(plan.enrollment.patient.full_name),
                bundles,
            )
        )
    return tuple(rows)


def _bundles(clinic_id: UUID, zone: ZoneInfo) -> tuple[BundleRow, ...]:
    pending: dict[UUID, list[CareProposal]] = {}
    for item in CareProposal.objects.filter(
        clinic_id=clinic_id, audience="administrative", state="pending"
    ).order_by("created_at"):
        pending.setdefault(item.invocation_id, []).append(item)
    return tuple(
        BundleRow(invocation, _minute(items[0].created_at, zone), len(items))
        for invocation, items in pending.items()
    )


def _automation(clinic_id: UUID) -> bool:
    with connection.cursor() as cursor:
        cursor.execute("SELECT clinic_app.agent_staff(%s)", [clinic_id])
        row = cursor.fetchone()
    return row is not None and row[0] is True


def _context(clinic_id: UUID, **extra: object) -> dict[str, object]:
    automation = _automation(clinic_id)
    clinician = _clinician(clinic_id)
    if not automation and not clinician:
        raise CareAccessDeniedError
    # Approving also needs tasks.assign; the Automations staff without it see
    # the page and why they cannot approve, never another person's rows.
    staff = automation and _care_staff(clinic_id)
    zone = _zone(clinic_id)
    context: dict[str, object] = {
        "clinic_id": clinic_id,
        "staff": staff,
        "automation": automation,
        "clinician": clinician,
        "plans": _plans(clinic_id, zone) if clinician else (),
        "bundles": _bundles(clinic_id, zone) if staff else (),
    }
    context.update(extra)
    return context


def _bundle_context(clinic_id: UUID, bundle: CareBundle) -> dict[str, object]:
    return {"bundle": bundle, "items": _rows(clinic_id, bundle.items)}


@privileged_totp_required(_url)
@sensitive_post_parameters()
@require_http_methods(["GET", "POST"])
def care_coordination(request: HttpRequest, clinic_id: UUID) -> HttpResponseBase:
    """Request, review and decide care-coordination proposals."""
    try:
        response = _page(request, clinic_id)
    except (
        CareAccessDeniedError,
        CurrentActorError,
        ActionAccessDeniedError,
        GatewayAccessDeniedError,
    ):
        return render(request, "403.html", status=403)
    except StepUpRequired:
        return flow_redirect(request, "identity:step-up", _url(clinic_id))
    except (
        CareConflictError,
        ActionRefusedError,
        ActionInputError,
        ValueError,
        ValidationError,
    ):
        response = _render(
            request,
            clinic_id,
            status=409,
            error=_(
                "These suggestions changed or are no longer eligible. "
                "Review them again or continue manually."
            ),
        )
    except SecretUnavailableError:
        response = _render(
            request,
            clinic_id,
            status=503,
            error=_(
                "Approval is unavailable. "
                "Ask an administrator to check its managed key."
            ),
        )
    response["Cache-Control"] = "private, no-store"
    return response


def _render(
    request: HttpRequest, clinic_id: UUID, *, status: int = 200, **extra: object
) -> HttpResponse:
    try:
        context = _context(clinic_id, **extra)
    except (CareAccessDeniedError, CurrentActorError):
        return render(request, "403.html", status=403)
    return render(request, "ai/care_coordination.html", context, status=status)


def _uuid(request: HttpRequest, name: str) -> UUID:
    return UUID(request.POST.get(name, ""))


def _request(request: HttpRequest, clinic_id: UUID) -> dict[str, object]:
    bundle = request_care_coordination(
        clinic_id=clinic_id, plan_id=_uuid(request, "plan_id")
    )
    return _bundle_context(clinic_id, bundle)


def _refresh(request: HttpRequest, clinic_id: UUID) -> dict[str, object]:
    bundle = collect_care_coordination(
        clinic_id=clinic_id, invocation_id=_uuid(request, "invocation_id")
    )
    return _bundle_context(clinic_id, bundle)


def _open(request: HttpRequest, clinic_id: UUID) -> dict[str, object]:
    bundle = care_bundle(
        clinic_id=clinic_id, invocation_id=_uuid(request, "invocation_id")
    )
    return _bundle_context(clinic_id, bundle)


def _preview(request: HttpRequest, clinic_id: UUID) -> dict[str, object]:
    invocation = _uuid(request, "invocation_id")
    items = pending_bundle(clinic_id=clinic_id, invocation_id=invocation)
    if not items:
        raise CareConflictError
    return {
        "preview": invocation,
        "preview_items": _rows(clinic_id, items),
        "digest": bundle_digest(
            clinic_id=clinic_id, invocation_id=invocation, items=items
        ),
    }


def _approve(request: HttpRequest, clinic_id: UUID) -> dict[str, object]:
    assert_step_up(request)
    outcomes = approve_care_bundle(
        clinic_id=clinic_id,
        invocation_id=_uuid(request, "invocation_id"),
        digest=request.POST.get("digest", ""),
        step_up=lambda digest: step_up_evidence(
            request, clinic_id=clinic_id, digest=digest
        ),
    )
    return {"outcomes": _outcomes(outcomes)}


def _decide(request: HttpRequest, clinic_id: UUID) -> dict[str, object]:
    decision = request.POST.get("decision", "")
    if decision not in ("accept", "reject"):
        raise CareConflictError
    item = decide_care_item(
        clinic_id=clinic_id,
        item_id=_uuid(request, "item_id"),
        decision="accept" if decision == "accept" else "reject",
        expected_revision=int(request.POST.get("revision", "")),
    )
    bundle = care_bundle(clinic_id=clinic_id, invocation_id=item.invocation_id)
    return {**_bundle_context(clinic_id, bundle), "saved": True}


ACTIONS: Final = {
    "request": _request,
    "refresh": _refresh,
    "open": _open,
    "preview": _preview,
    "approve": _approve,
    "decide": _decide,
}


@transaction.atomic
def _page(request: HttpRequest, clinic_id: UUID) -> HttpResponse:
    if request.method == "GET":
        return render(request, "ai/care_coordination.html", _context(clinic_id))
    handler = ACTIONS.get(request.POST.get("action", ""))
    if (
        handler is None
        or request.FILES
        or set(request.POST) - _FIELDS
        or any(len(request.POST.getlist(key)) != 1 for key in request.POST)
    ):
        return render(request, "400.html", status=400)
    extra = handler(request, clinic_id)
    return render(request, "ai/care_coordination.html", _context(clinic_id, **extra))


def _outcomes(outcomes: tuple[ItemOutcome, ...]) -> tuple[dict[str, object], ...]:
    items = {
        item.pk: item
        for item in CareProposal.objects.filter(
            pk__in=[outcome.item_id for outcome in outcomes]
        )
    }
    return tuple(
        {
            "item_id": outcome.item_id,
            "state": outcome.state,
            "kind_label": _(KIND_LABELS[items[outcome.item_id].kind]),
            "message": _(STATE_LABELS[outcome.state])
            if outcome.state in STATE_LABELS
            else _(REFUSAL_LABEL),
        }
        for outcome in outcomes
        if outcome.item_id in items
    )
