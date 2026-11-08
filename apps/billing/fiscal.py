"""Finance-only fiscal drafts and offline rehearsals, never payment receipts."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING
from uuid import UUID, uuid4, uuid5

from config.settings.contracts import resolved_data_mode
from django.conf import settings
from django.utils import timezone

from apps.audit.services import record_phase1_event
from apps.billing.errors import BillingAccessDeniedError
from apps.billing.fiscal_policy import (
    FiscalRefusal,
    FiscalRefusedError,
    FiscalReview,
    require_fiscal_issuance,
    require_transition,
    validate_review,
)
from apps.billing.fiscal_provider import FiscalRequest
from apps.billing.fiscal_rehearsal import rehearsal_provider
from apps.billing.models import (
    FiscalConfiguration,
    FiscalDocument,
    FiscalTransition,
    Invoice,
)
from apps.billing.security import book_write

if TYPE_CHECKING:
    from apps.billing.fiscal_provider import FiscalResponse


def _review(configuration: FiscalConfiguration) -> FiscalReview:
    return FiscalReview(
        legal_entity_reference=configuration.legal_entity_reference,
        municipality_code=configuration.municipality_code,
        service_code=configuration.service_code,
        tax_regime=configuration.tax_regime,
        ibs_cbs_applicability=configuration.ibs_cbs_applicability,
        tax_fields=configuration.tax_fields,
        review_reference=configuration.review_reference,
        reviewed_at=configuration.reviewed_at,
        expires_at=configuration.expires_at,
        review_kind=configuration.review_kind,
        cancellation_window_seconds=configuration.cancellation_window_seconds,
    )


def _document(*, clinic_id: UUID, document_id: UUID) -> FiscalDocument:
    try:
        return FiscalDocument.objects.select_related("configuration").get(
            pk=document_id, clinic_id=clinic_id
        )
    except FiscalDocument.DoesNotExist as error:
        raise BillingAccessDeniedError from error


def _command(document: FiscalDocument, command_id: UUID, target: str) -> bool:
    if type(command_id) is not UUID:
        raise FiscalRefusedError(FiscalRefusal.COMMAND_CONFLICT)
    previous = FiscalTransition.objects.filter(
        command_id=command_id,
        document_id=document.pk,
        document__clinic_id=document.clinic_id,
        document__book_id=document.book_id,
    ).first()
    if previous is None:
        return False
    if previous.to_status != target:
        raise FiscalRefusedError(FiscalRefusal.COMMAND_CONFLICT)
    return True


def _request(document: FiscalDocument) -> FiscalRequest:
    return FiscalRequest(
        reference=document.pk,
        dps_id=document.dps_id,
        signed_xml=document.request_xml,
    )


def _require_rehearsal(document: FiscalDocument) -> None:
    validate_review(_review(document.configuration), now=timezone.now(), rehearsal=True)
    if (
        not document.rehearsal
        or resolved_data_mode() != "synthetic"
        or getattr(settings, "BILLING_SYNTHETIC_NFSE", False) is not True
    ):
        raise FiscalRefusedError(FiscalRefusal.BLOCKED_ON_EG)


def _transition(
    document: FiscalDocument,
    *,
    status: str,
    command_id: UUID,
    fields: tuple[str, ...] = (),
) -> FiscalDocument:
    require_transition(
        status=document.status,
        next_status=status,
        now=timezone.now(),
        cancel_until=document.cancel_until,
    )
    document.status = status
    document.last_command_id = command_id
    document.save(update_fields=("status", "last_command_id", *fields))
    document.refresh_from_db()
    record_phase1_event(
        f"billing.fiscal.{status}",
        clinic_id=document.clinic_id,
        affected_record_id=document.pk,
    )
    return document


def prepare_fiscal_document(
    *,
    clinic_id: UUID,
    invoice_id: UUID,
    configuration_id: UUID,
    command_id: UUID,
) -> FiscalDocument:
    """Freeze a reviewed configuration and charge; never create a fiscal approval."""
    if type(command_id) is not UUID:
        raise FiscalRefusedError(FiscalRefusal.COMMAND_CONFLICT)
    fingerprint = hashlib.sha256(
        b"clinic-fiscal-create-v1\0" + invoice_id.bytes + configuration_id.bytes
    ).digest()
    with book_write(clinic_id=clinic_id, permission="settlement.post") as book:
        existing = FiscalDocument.objects.filter(
            create_command_id=command_id, clinic_id=clinic_id, book=book
        ).first()
        if existing is not None:
            if bytes(existing.create_fingerprint) != fingerprint:
                raise FiscalRefusedError(FiscalRefusal.COMMAND_CONFLICT)
            return existing
        try:
            configuration = FiscalConfiguration.objects.get(
                pk=configuration_id, book=book, clinic_id=clinic_id
            )
            invoice = Invoice.objects.get(
                pk=invoice_id, clinic_id=clinic_id, state__in=("open", "paid")
            )
        except (FiscalConfiguration.DoesNotExist, Invoice.DoesNotExist) as error:
            raise BillingAccessDeniedError from error
        rehearsal = configuration.review_kind == "synthetic_fixture"
        validate_review(_review(configuration), now=timezone.now(), rehearsal=rehearsal)
        if not configuration.legal_entity:
            raise FiscalRefusedError(FiscalRefusal.CONFIGURATION_INVALID)
        if not rehearsal:
            require_fiscal_issuance(
                clinic_id=clinic_id, review=_review(configuration), now=timezone.now()
            )
        reference = uuid4()
        dps_id = f"DPS-SYNTHETIC-{reference.hex}"
        document = FiscalDocument.objects.create(
            id=reference,
            organization_id=book.organization_id,
            clinic_id=clinic_id,
            book=book,
            configuration=configuration,
            invoice=invoice,
            legal_entity_reference=configuration.legal_entity_reference,
            provider=configuration.provider,
            municipality_code=configuration.municipality_code,
            service_code=configuration.service_code,
            tax_fields=configuration.tax_fields,
            amount_minor=invoice.amount_minor,
            rehearsal=rehearsal,
            create_command_id=command_id,
            create_fingerprint=fingerprint,
            last_command_id=command_id,
            dps_id=dps_id,
            request_xml=(
                f'<DPS Id="{dps_id}"><rehearsal>SYNTHETIC-NOT-FISCAL</rehearsal></DPS>'
            ).encode("ascii"),
            rejection_code="",
        )
        record_phase1_event(
            "billing.fiscal.draft", clinic_id=clinic_id, affected_record_id=document.pk
        )
        return document


def submit_fiscal_document(
    *, clinic_id: UUID, document_id: UUID, command_id: UUID
) -> FiscalDocument:
    """Fail closed without EG-9, activated nfse and an approved live implementation."""
    if type(command_id) is not UUID:
        raise FiscalRefusedError(FiscalRefusal.COMMAND_CONFLICT)
    with book_write(clinic_id=clinic_id, permission="settlement.post"):
        document = _document(clinic_id=clinic_id, document_id=document_id)
        require_fiscal_issuance(
            clinic_id=clinic_id,
            review=_review(document.configuration),
            now=timezone.now(),
        )
        # require_fiscal_issuance always refuses this source-only live leg.
        raise FiscalRefusedError(FiscalRefusal.BLOCKED_ON_EG)


def _record_outcome(
    document: FiscalDocument, outcome: FiscalResponse, command_id: UUID
) -> FiscalDocument:
    if outcome.dps_id != document.dps_id:
        raise FiscalRefusedError(FiscalRefusal.PROVIDER_RESPONSE_INVALID)
    if outcome.status == "submitted":
        return document
    fields: tuple[str, ...]
    if outcome.status == "authorized" and outcome.xml and outcome.provider_reference:
        document.xml_payload = outcome.xml
        document.xml_reference = document.pk
        document.pdf_payload = outcome.pdf or None
        document.pdf_reference = document.pk if outcome.pdf else None
        document.provider_reference = outcome.provider_reference
        fields = (
            "xml_payload",
            "xml_reference",
            "pdf_payload",
            "pdf_reference",
            "provider_reference",
        )
    elif outcome.status == "rejected" and outcome.rejection_code:
        document.rejection_code = outcome.rejection_code
        fields = ("rejection_code",)
    else:
        raise FiscalRefusedError(FiscalRefusal.PROVIDER_RESPONSE_INVALID)
    return _transition(
        document,
        status=outcome.status,
        command_id=uuid5(command_id, "fiscal-result-v1"),
        fields=fields,
    )


def submit_fiscal_rehearsal(
    *, clinic_id: UUID, document_id: UUID, command_id: UUID
) -> FiscalDocument:
    """Run only a deterministic local fake; ambiguous retries query the same DPS."""
    with book_write(clinic_id=clinic_id, permission="settlement.post"):
        document = _document(clinic_id=clinic_id, document_id=document_id)
        _require_rehearsal(document)
        replay = _command(document, command_id, "submitted")
        if replay and document.status != "submitted":
            return document
        if not replay:
            document = _transition(document, status="submitted", command_id=command_id)
        request = _request(document)
        provider = rehearsal_provider(request=request, provider=document.provider)
        try:
            outcome = (
                provider.lookup(request=request)
                if replay
                else provider.submit(request=request)
            )
        except (TimeoutError, FiscalRefusedError):
            # No negative fiscal conclusion can be drawn from a timeout/bad response.
            return document
        return _record_outcome(document, outcome, command_id)


def cancel_fiscal_rehearsal(
    *, clinic_id: UUID, document_id: UUID, command_id: UUID
) -> FiscalDocument:
    """Cancel a local draft or recorded rehearsal, never infer a statutory window."""
    with book_write(clinic_id=clinic_id, permission="settlement.post"):
        document = _document(clinic_id=clinic_id, document_id=document_id)
        _require_rehearsal(document)
        if _command(document, command_id, "cancelled"):
            return document
        require_transition(
            status=document.status,
            next_status="cancelled",
            now=timezone.now(),
            cancel_until=document.cancel_until,
        )
        fields: tuple[str, ...] = ()
        if document.status == "authorized":
            request = _request(document)
            provider = rehearsal_provider(request=request, provider=document.provider)
            outcome = provider.cancel(
                request=request,
                provider_reference=document.provider_reference,
                signed_event=(
                    f'<evento command="{command_id}">'
                    "SYNTHETIC-NOT-FISCAL-CANCELLED</evento>"
                ).encode("ascii"),
            )
            if (
                outcome.status != "cancelled"
                or outcome.dps_id != document.dps_id
                or not outcome.xml
            ):
                raise FiscalRefusedError(FiscalRefusal.PROVIDER_RESPONSE_INVALID)
            document.cancellation_payload = outcome.xml
            fields = ("cancellation_payload",)
        return _transition(
            document, status="cancelled", command_id=command_id, fields=fields
        )
