"""Author-only prescription preparation; real issuance remains unavailable."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from hashlib import sha256
from typing import TYPE_CHECKING, Any, NoReturn, TypedDict, Unpack

import rfc8785
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.utils import timezone, translation

from apps.audit.canonical import AuditEventInput
from apps.audit.services import record_event, record_phase1_event
from apps.ehr.models import Encounter
from apps.ehr.services import (
    ClinicalAccessDeniedError,
    ClinicalConflictError,
    _denied,
    record_denial,
)
from apps.identity.clinic_configuration import latest_configuration
from apps.identity.current_context import (
    CurrentActorError,
    current_actor_username,
    require_current_actor_clinic_roles,
)
from apps.identity.models import Clinic, UserClinicRole
from apps.intake.models import Patient
from apps.orders.values import CATALOG
from apps.prescription.class_values import CLASSES_CONTRACT, PrescriptionClass
from apps.prescription.directions import item_snapshot, snapshot_items
from apps.prescription.document_rendering import (
    DOCUMENT_INPUT_VERSION,
    RENDER_PROFILE,
    RENDERER_ID,
    DocumentRenderer,
    DocumentRenderError,
    default_renderer,
    validate_pdf_structure,
)
from apps.prescription.document_templates import TEMPLATE_VERSION
from apps.prescription.documents import (
    DocumentContentInput,
    _enrollment,
    document_content,
    exam_order_command,
)
from apps.prescription.models import (
    DocumentDraftContent,
    DocumentKind,
    PrescriptionDocument,
    PrescriptionDraft,
    PrescriptionItem,
)
from apps.prescription.policy import (
    DOSE_UNIT_LABELS,
    SYNTHETIC_CATEGORY,
    validate_category,
    validate_items,
)

if TYPE_CHECKING:
    from uuid import UUID


class EncounterAssertions(TypedDict):
    """Required untrusted scope assertions, never caller-supplied authority."""

    encounter_id: UUID
    patient_id: UUID
    issuer_id: UUID


def issue_prescription() -> NoReturn:
    """Issue a prescription when the prescription domain is implemented."""
    message = "Phase >=1"
    raise NotImplementedError(message)


def authorize_encounter(*, clinic_id: UUID, encounter_id: UUID) -> Encounter:
    """Resolve canonical role and assignment; caller identity is never authority."""
    actor = require_current_actor_clinic_roles(
        clinic_id, (UserClinicRole.Role.PHYSICIAN,)
    )
    encounter = (
        Encounter.objects.select_related("appointment", "patient")
        .filter(pk=encounter_id, clinic_id=clinic_id)
        .first()
    )
    if encounter is None:
        raise ClinicalAccessDeniedError
    # Unscheduled encounters (plan item 27) have no appointment; prescribing
    # from them stays refused until the prescription contract admits them.
    if (
        encounter.physician_id != actor
        or encounter.appointment is None
        or encounter.appointment.practitioner_id != actor
    ):
        record_denial(clinic_id, encounter.pk, "not_assigned")
        raise ClinicalAccessDeniedError
    return encounter


def _event(draft: PrescriptionDraft, action: str) -> None:
    record_event(
        AuditEventInput(
            event_type=f"prescription.draft.{action}",
            component_id="clinic-os-web",
            component_ip=None,
            affected_record_type="prescription.draft",
            affected_record_id=str(draft.pk),
            occurred_at_utc=timezone.now(),
        ),
        payload={"clinic_id": str(draft.clinic_id), "object_verb": action},
    )


def _scope(encounter: Encounter, patient_id: UUID, issuer_id: UUID) -> None:
    if encounter.patient_id != patient_id or encounter.physician_id != issuer_id:
        record_denial(encounter.clinic_id, encounter.pk, "not_assigned")
        raise ClinicalAccessDeniedError


def create_draft(
    *,
    clinic_id: UUID,
    encounter_id: UUID,
    patient_id: UUID,
    issuer_id: UUID,
    category: str | None = None,
) -> PrescriptionDraft:
    """Create or resume a draft without choosing a class or replacing content."""
    encounter = authorize_encounter(clinic_id=clinic_id, encounter_id=encounter_id)
    _scope(encounter, patient_id, issuer_id)
    contract = validate_category(category)
    with transaction.atomic():
        encounter = Encounter.objects.select_for_update().get(pk=encounter.pk)
        existing = PrescriptionDraft.objects.filter(
            encounter=encounter, kind=DocumentKind.PRESCRIPTION
        ).first()
        if existing is not None:
            if existing.state != "draft":
                reason = "precondition_failed"
                raise ClinicalConflictError(reason)
            _event(existing, "viewed")
            return existing
        if encounter.state != "open":
            reason = "encounter_closed"
            raise ClinicalConflictError(reason)
        draft = PrescriptionDraft.objects.create(
            organization_id=encounter.organization_id,
            clinic_id=clinic_id,
            encounter=encounter,
            patient_id=patient_id,
            issuer_id=issuer_id,
            category=category,
            contract_version=contract,
        )
        _event(draft, "created")
        return draft


def view_draft(
    *,
    clinic_id: UUID,
    draft_id: UUID,
    encounter_id: UUID,
    patient_id: UUID,
    issuer_id: UUID,
) -> PrescriptionDraft:
    """Resume only the exact encounter/patient/issuer binding under FORCE RLS."""
    encounter = authorize_encounter(clinic_id=clinic_id, encounter_id=encounter_id)
    _scope(encounter, patient_id, issuer_id)
    draft = PrescriptionDraft.objects.filter(
        pk=draft_id,
        encounter=encounter,
        patient_id=patient_id,
        issuer_id=issuer_id,
        state="draft",
    ).first()
    if draft is None:
        raise ClinicalAccessDeniedError
    validate_category(draft.category)
    _event(draft, "viewed")
    return draft


def save_draft(
    *,
    clinic_id: UUID,
    draft_id: UUID,
    category: str | None,
    expected_version: int,
    items: list[dict[str, str]],
    **scope: Unpack[EncounterAssertions],
) -> PrescriptionDraft:
    """Lock, compare and append a complete snapshot; failed writes change nothing."""
    authorized = view_draft(clinic_id=clinic_id, draft_id=draft_id, **scope)
    if authorized.kind != DocumentKind.PRESCRIPTION:
        raise ClinicalAccessDeniedError
    contract = validate_category(category)
    validate_items(items)
    with transaction.atomic():
        draft = _locked_draft(authorized.pk, expected_version)
        draft.version += 1
        draft.category = category
        draft.contract_version = contract
        snapshots = [
            item_snapshot(draft=draft, item=item, position=position)
            for position, item in enumerate(items, start=1)
        ]
        draft.save(
            update_fields=("version", "updated_at", "category", "contract_version")
        )
        PrescriptionItem.objects.bulk_create(snapshots)
        _event(draft, "saved")
        return draft


def _locked_draft(draft_id: UUID, expected_version: int) -> PrescriptionDraft:
    draft = PrescriptionDraft.objects.select_for_update().filter(pk=draft_id).first()
    if draft is None or draft.state != "draft" or draft.version != expected_version:
        reason = "stale_revision"
        raise ClinicalConflictError(reason)
    return draft


VERIFICATION_BASE_URL = "https://verify.clinic-os.invalid/v/"
QR_HANDLE_BYTES = 32


def create_document_draft(
    *, clinic_id: UUID, encounter_id: UUID, kind: str
) -> PrescriptionDraft:
    """Create/resume one kind without changing the pinned prescription service."""
    encounter = authorize_encounter(clinic_id=clinic_id, encounter_id=encounter_id)
    _enrollment(encounter)
    if not isinstance(kind, str) or kind not in DocumentKind.values:
        raise ValidationError(
            translation.gettext("Choose a document kind."), code="invalid_kind"
        )
    with transaction.atomic():
        encounter = Encounter.objects.select_for_update().get(pk=encounter.pk)
        existing = PrescriptionDraft.objects.filter(
            encounter=encounter, kind=kind
        ).first()
        if existing is not None:
            if existing.state != "draft":
                reason = "precondition_failed"
                raise ClinicalConflictError(reason)
            return existing
        if encounter.state != "open":
            reason = "encounter_closed"
            raise ClinicalConflictError(reason)
        category = None if kind == DocumentKind.PRESCRIPTION else SYNTHETIC_CATEGORY
        draft = PrescriptionDraft.objects.create(
            organization_id=encounter.organization_id,
            clinic_id=clinic_id,
            encounter=encounter,
            patient_id=encounter.patient_id,
            issuer_id=encounter.physician_id,
            kind=kind,
            category=category,
            contract_version=validate_category(category),
        )
        record_phase1_event(
            "prescription.clinical_draft.created",
            clinic_id=clinic_id,
            affected_record_id=draft.pk,
        )
        return draft


def authorize_document_draft(
    *, clinic_id: UUID, draft_id: UUID, permission: str = "prescription.prepare"
) -> PrescriptionDraft:
    """Recheck assigned physician, current professional scope and patient care."""
    draft = PrescriptionDraft.objects.filter(pk=draft_id, clinic_id=clinic_id).first()
    if draft is None:
        raise ClinicalAccessDeniedError
    encounter = authorize_encounter(
        clinic_id=clinic_id, encounter_id=draft.encounter_id
    )
    _enrollment(encounter, permission=permission)
    return draft


def save_document_draft(
    *,
    clinic_id: UUID,
    draft_id: UUID,
    expected_version: int,
    content: DocumentContentInput,
) -> PrescriptionDraft:
    """Append exact text and advance the version; no old review remains current."""
    authorized = authorize_document_draft(clinic_id=clinic_id, draft_id=draft_id)
    if authorized.kind == DocumentKind.PRESCRIPTION:
        raise ClinicalAccessDeniedError
    if type(expected_version) is not int or expected_version < 1:
        raise ValidationError(
            translation.gettext("Check the clinical document fields.")
        )
    payload = content.payload(authorized.kind)
    with transaction.atomic():
        draft = _locked_draft(draft_id, expected_version)
        draft.version += 1
        draft.save(update_fields=("version", "updated_at"))
        DocumentDraftContent.objects.create(
            organization_id=draft.organization_id,
            draft=draft,
            version=draft.version,
            content=payload,
        )
        record_phase1_event(
            "prescription.clinical_draft.saved",
            clinic_id=clinic_id,
            affected_record_id=draft.pk,
        )
        return draft


class DocumentStorageError(Exception):
    """Report stored artifact bytes that fail their recorded digest."""


@dataclass(frozen=True, kw_only=True)
class DocumentDownload:
    """Fully materialized, authorized artifact bytes for a bounded response."""

    data: bytes
    content_type: str
    file_name: str


@dataclass(frozen=True, kw_only=True)
class DocumentIntegrity:
    """The digest verdict for one stored artifact; never a repair."""

    ok: bool
    reason_code: str
    input_digest: str
    pdf_digest: str


def _document_event(document: PrescriptionDocument, action: str) -> None:
    record_event(
        AuditEventInput(
            event_type=f"prescription.document.{action}",
            component_id="clinic-os-web",
            component_ip=None,
            affected_record_type="prescription.document",
            affected_record_id=str(document.pk),
            occurred_at_utc=timezone.now(),
        ),
        payload={"clinic_id": str(document.clinic_id), "object_verb": action},
    )


def _frozen_input(
    draft: PrescriptionDraft,
    *,
    verification_url: str,
    issued_at: str,
) -> dict[str, Any]:
    """Snapshot every rendered field; the digest covers exactly this mapping."""
    patient = Patient.objects.filter(pk=draft.patient_id).first()
    clinic = Clinic.objects.filter(pk=draft.clinic_id).first()
    if patient is None or clinic is None:
        raise ClinicalAccessDeniedError
    configuration = latest_configuration(draft.clinic_id)
    items = draft_items(draft)
    frozen = {
        "v": DOCUMENT_INPUT_VERSION,
        "draft_id": str(draft.pk),
        "document_version": draft.version,
        "contract_version": draft.contract_version,
        "category": draft.category,
        "issuer_label": current_actor_username(),
        "patient_label": patient.full_name,
        "clinic_label": configuration.display_name if configuration else clinic.name,
        "kind": draft.kind,
        "template_version": TEMPLATE_VERSION,
        "branding": {
            "version": configuration.version if configuration else 0,
            "contact_email": configuration.contact_email if configuration else "",
            "contact_phone": configuration.contact_phone if configuration else "",
            "brand_token": configuration.brand_token if configuration else "navy",
        },
        "issued_at": issued_at,
        "verification_url": verification_url,
        "items": items,
    }
    if draft.kind != DocumentKind.PRESCRIPTION:
        content = document_content(draft)
        with translation.override("pt-br"):
            frozen["content"] = {
                **content,
                "exam_labels": [
                    f"{CATALOG[code]!s} ({code})" for code in content["exam_codes"]
                ],
            }
        if draft.kind == DocumentKind.EXAM_REQUEST:
            frozen["order_command_id"] = str(exam_order_command(draft))
    with translation.override("pt-br"):
        for item in items:
            if item.get("dose_unit"):
                item["dose_unit_label"] = str(DOSE_UNIT_LABELS[item["dose_unit"]])
                item["directions_label"] = translation.gettext(
                    "Frequency is administrations per day; duration is days."
                )
        if draft.contract_version == CLASSES_CONTRACT:
            frozen["class_label"] = (
                str(PrescriptionClass(draft.category).label)
                if draft.category
                else translation.gettext("No class selected")
            )
            frozen["preparation_mode"] = "paper"
            frozen["preparation_label"] = translation.gettext(
                "Paper preparation only; not issued."
            )
    return frozen


def _input_digest(frozen: dict[str, Any]) -> str:
    """Digest the canonical frozen input minus the digest field itself."""
    content = {key: value for key, value in frozen.items() if key != "input_digest"}
    return sha256(rfc8785.dumps(content)).hexdigest()


def _render_params(verification_url: str) -> dict[str, Any]:
    """Record the exact render contract bound to the produced bytes."""
    return {
        "renderer": RENDERER_ID,
        "profile": RENDER_PROFILE,
        "page": "a4",
        "font": "helvetica-11",
        "qr": {"ecc": "M", "border": 4, "module_pt": 3},
        "verification_url": verification_url,
    }


def _validate_rendered(pdf_bytes: bytes, pdf_digest: str) -> None:
    """Reject malformed or mislabeled output before any artifact exists."""
    validate_pdf_structure(pdf_bytes)
    if sha256(pdf_bytes).hexdigest() != pdf_digest:
        raise DocumentRenderError


def render_document(
    *,
    clinic_id: UUID,
    draft_id: UUID,
    expected_version: int,
    renderer: DocumentRenderer | None = None,
    **scope: Unpack[EncounterAssertions],
) -> PrescriptionDocument:
    """Freeze the exact draft version into one immutable rendered artifact.

    The draft row stays locked for the whole operation, so the rendered
    version cannot change underneath. The document version, render
    parameters, frozen input and both digests are bound in the same row
    before any signing lifecycle exists. Rendering or dependency failures
    write nothing.
    """
    view_draft(
        clinic_id=clinic_id,
        draft_id=draft_id,
        encounter_id=scope["encounter_id"],
        patient_id=scope["patient_id"],
        issuer_id=scope["issuer_id"],
    )
    engine = renderer if renderer is not None else default_renderer()
    with transaction.atomic():
        draft = _locked_draft(draft_id, expected_version)
        if PrescriptionDocument.objects.filter(
            draft=draft, document_version=draft.version
        ).exists():
            reason = "already_rendered"
            raise ClinicalConflictError(reason)
        if draft.kind != DocumentKind.PRESCRIPTION:
            authorize_document_draft(clinic_id=clinic_id, draft_id=draft.pk)
        handle = secrets.token_urlsafe(QR_HANDLE_BYTES)
        verification_url = f"{VERIFICATION_BASE_URL}{handle}"
        issued_at = timezone.now().isoformat()
        frozen = _frozen_input(
            draft,
            verification_url=verification_url,
            issued_at=issued_at,
        )
        frozen["input_digest"] = _input_digest(frozen)
        rendered = engine.render(frozen)
        _validate_rendered(rendered.pdf_bytes, rendered.pdf_digest)
        document = PrescriptionDocument.objects.create(
            organization_id=draft.organization_id,
            draft=draft,
            encounter_id=draft.encounter_id,
            clinic_id=clinic_id,
            patient_id=draft.patient_id,
            issuer_id=draft.issuer_id,
            document_version=draft.version,
            state=PrescriptionDocument.State.RENDERED,
            render_params=_render_params(verification_url),
            frozen_input=frozen,
            input_digest=frozen["input_digest"],
            pdf_digest=rendered.pdf_digest,
            pdf_bytes=rendered.pdf_bytes,
            qr_handle=handle,
        )
        _document_event(document, "rendered")
        return document


def _authorize_document_care(clinic_id: UUID, encounter_id: UUID) -> None:
    """Recheck the clinical read predicate for the artifact's encounter."""
    encounter = Encounter.objects.filter(pk=encounter_id, clinic_id=clinic_id).first()
    if encounter is None:
        raise ClinicalAccessDeniedError
    try:
        require_current_actor_clinic_roles(clinic_id, (UserClinicRole.Role.PHYSICIAN,))
    except CurrentActorError:
        _denied(clinic_id, encounter_id, "role_denied")
    with connection.cursor() as cursor:
        cursor.execute("SELECT clinic_app.ehr_care(%s)", [encounter_id])
        care = cursor.fetchone()
    if not care or not care[0]:
        _denied(clinic_id, encounter_id, "no_care_relationship")


def download_document(*, clinic_id: UUID, document_id: UUID) -> DocumentDownload:
    """Authorize by role and care relationship, then serve exact bytes.

    The QR handle is never accepted here: it is a public verification token,
    not download authority. Stored bytes are re-checked against the recorded
    digest before they leave the tenant boundary.
    """
    document = PrescriptionDocument.objects.filter(
        pk=document_id, clinic_id=clinic_id
    ).first()
    if document is None:
        raise ClinicalAccessDeniedError
    _authorize_document_care(clinic_id, document.encounter_id)
    data = bytes(document.pdf_bytes)
    if sha256(data).hexdigest() != document.pdf_digest:
        raise DocumentStorageError
    kind = document.frozen_input.get("kind", DocumentKind.PRESCRIPTION)
    if not isinstance(kind, str) or kind not in DocumentKind.values:
        raise DocumentStorageError
    prefix = "prescricao" if kind == DocumentKind.PRESCRIPTION else kind
    _document_event(document, "downloaded")
    return DocumentDownload(
        data=data,
        content_type="application/pdf",
        file_name=f"{prefix}-{document.pk}.pdf",
    )


def verify_document_integrity(document: PrescriptionDocument) -> DocumentIntegrity:
    """Recompute both digests from stored content; mismatches never repair."""
    recomputed_input = _input_digest(document.frozen_input)
    recomputed_pdf = sha256(bytes(document.pdf_bytes)).hexdigest()
    if recomputed_input != document.input_digest:
        return DocumentIntegrity(
            ok=False,
            reason_code="input_digest_mismatch",
            input_digest=recomputed_input,
            pdf_digest=recomputed_pdf,
        )
    if recomputed_pdf != document.pdf_digest:
        return DocumentIntegrity(
            ok=False,
            reason_code="pdf_digest_mismatch",
            input_digest=recomputed_input,
            pdf_digest=recomputed_pdf,
        )
    return DocumentIntegrity(
        ok=True,
        reason_code="verified",
        input_digest=recomputed_input,
        pdf_digest=recomputed_pdf,
    )


def require_current_document(document: PrescriptionDocument) -> None:
    """Fence approval/signing against edits and independently recheck bytes."""
    if (
        document.frozen_input.get("kind", DocumentKind.PRESCRIPTION)
        != DocumentKind.PRESCRIPTION
    ):
        authorize_document_draft(
            clinic_id=document.clinic_id,
            draft_id=document.draft_id,
            permission="prescription.sign",
        )
    draft = (
        PrescriptionDraft.objects.select_for_update()
        .filter(pk=document.draft_id, state="draft")
        .first()
    )
    if draft is None or draft.version != document.document_version:
        reason = "stale_document_approval"
        raise ClinicalConflictError(reason)
    if not verify_document_integrity(document).ok:
        raise DocumentStorageError


def draft_items(draft: PrescriptionDraft) -> list[dict[str, str]]:
    """Return the current author-visible snapshot, never discarded content."""
    return snapshot_items(draft)


def discard_draft(
    *,
    clinic_id: UUID,
    draft_id: UUID,
    expected_version: int,
    **scope: Unpack[EncounterAssertions],
) -> None:
    """Retain an unwanted draft as terminal metadata, allowing encounter closure."""
    authorized = view_draft(clinic_id=clinic_id, draft_id=draft_id, **scope)
    with transaction.atomic():
        draft = _locked_draft(authorized.pk, expected_version)
        draft.state = "discarded"
        draft.version += 1
        draft.save(update_fields=("state", "version", "updated_at"))
        if draft.kind == DocumentKind.PRESCRIPTION:
            _event(draft, "discarded")
        else:
            record_phase1_event(
                "prescription.clinical_draft.discarded",
                clinic_id=clinic_id,
                affected_record_id=draft.pk,
            )
