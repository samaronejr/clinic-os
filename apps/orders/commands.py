"""Validate native form commands before calling independently authorized services."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _

from apps.ehr.attachments import MAX_ATTACHMENT_BYTES
from apps.orders.forms import OrderForm, UploadForm
from apps.orders.models import Order
from apps.orders.services import (
    IdentityEvidence,
    ObservationInput,
    OrdersAccessDeniedError,
    ResultInput,
    acknowledge_result,
    ingest_result,
    match_result,
    place_order,
    reject_result,
    release_result,
)

if TYPE_CHECKING:
    from django.http import HttpRequest


def _upload(request: HttpRequest, clinic_id: UUID) -> str:
    form = UploadForm(request.POST, request.FILES)
    if not form.is_valid():
        raise ValidationError(_("Review the result source and identity fields."))
    values = form.cleaned_data
    if not values["value"] and any(
        values[field]
        for field in ("observation_code", "unit", "reference_interval", "abnormal_flag")
    ):
        raise ValidationError(
            _("Provide a source value or leave the observation fields blank.")
        )
    uploaded = values["source"]
    observations: tuple[ObservationInput, ...] = ()
    order = Order.objects.filter(pk=values["order_id"], clinic_id=clinic_id).first()
    if values["value"]:
        if order is None:
            raise OrdersAccessDeniedError
        if (
            not values["observation_code"]
            or not order.items.filter(exam_code=values["observation_code"]).exists()
        ):
            raise ValidationError(
                _("Choose the requested examination for the transcribed value.")
            )
        observations = (
            ObservationInput(
                code=values["observation_code"],
                value=values["value"],
                unit=values["unit"],
                reference_interval=values["reference_interval"],
                abnormal_flag=values["abnormal_flag"],
            ),
        )
    receipt = ingest_result(
        clinic_id=clinic_id,
        submission=ResultInput(
            order_id=values["order_id"],
            identity=IdentityEvidence(
                name=values["name"],
                birth_date=values["birth_date"],
                identifier_kind=values["identifier_kind"],
                identifier=values["identifier"],
            ),
            content_type=uploaded.content_type or "",
            data=uploaded.read(MAX_ATTACHMENT_BYTES + 1),
            event_id=values["event_id"],
            observations=observations,
            critical=values["critical"],
            result_id=values["result_id"],
            predecessor_id=values["predecessor_id"],
        ),
    )
    return (
        _("Result quarantined. The chart is unchanged; an operator task was created.")
        if receipt.state == "quarantined"
        else _("Result matched. Clinical acknowledgment is still required.")
    )


def submit_command(
    request: HttpRequest, clinic_id: UUID, patients: tuple[tuple[str, str], ...]
) -> str:
    """Validate the chosen command without combining review and release."""
    action = request.POST.get("action")
    if action == "place":
        form = OrderForm(patients, request.POST)
        if not form.is_valid():
            raise ValidationError(_("Choose the patient and requested examinations."))
        place_order(
            clinic_id=clinic_id,
            enrollment_id=UUID(form.cleaned_data["enrollment_id"]),
            exam_codes=tuple(form.cleaned_data["exam_codes"]),
            command_id=form.cleaned_data["command_id"],
        )
        return _("Order placed.")
    if action == "upload":
        return _upload(request, clinic_id)
    version = UUID(request.POST.get("version_id", ""))
    if action == "acknowledge":
        acknowledge_result(
            clinic_id=clinic_id,
            version_id=version,
            disposition=request.POST.get("disposition", ""),
        )
        return _("Clinical acknowledgment recorded for this version.")
    if action == "release":
        delegate = request.POST.get("delegate_grant_id", "")
        release_result(
            clinic_id=clinic_id,
            version_id=version,
            audience=request.POST.get("audience", ""),
            delegate_grant_id=UUID(delegate) if delegate else None,
        )
        return _("This version was released to the selected audience.")
    if action == "match":
        match_result(
            clinic_id=clinic_id,
            version_id=version,
            order_id=UUID(request.POST.get("order_id", "")),
        )
        outcome = _("Source identity verified.")
    elif action == "reject":
        reject_result(
            clinic_id=clinic_id,
            version_id=version,
            evidence_code=request.POST.get("evidence_code", ""),
        )
        outcome = _("Quarantined source rejected; original bytes retained.")
    else:
        raise OrdersAccessDeniedError
    return outcome
