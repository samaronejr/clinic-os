"""Clinical documents share the immutable prescription artifact and trust pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, TypedDict
from uuid import uuid5

from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _

from apps.ehr.services import ClinicalAccessDeniedError, ClinicalConflictError
from apps.identity.current_context import require_permission
from apps.intake.models import PatientClinicEnrollment
from apps.orders.services import place_order
from apps.orders.values import CATALOG
from apps.prescription.document_rendering import DocumentRenderError
from apps.prescription.models import (
    DocumentDraftContent,
    DocumentKind,
    PrescriptionDocument,
    PrescriptionDraft,
)

if TYPE_CHECKING:
    from uuid import UUID

    from apps.ehr.models import Encounter
    from apps.orders.models import Order

MAX_DOCUMENT_BODY: Final = 20000
MAX_RECIPIENT: Final = 240
CONTROL_FLOOR: Final = 32
CID10_SOURCE_RECORD: Final = "docs/clinical/document-terminology-source-v1.json"


class DocumentContent(TypedDict):
    """Closed, encrypted, versioned clinician-authored snapshot."""

    body: str
    recipient: str
    exam_codes: list[str]
    exam_catalog_version: int | None
    cid10_code: str
    cid10_source_version: str
    cid10_source_record: str


@dataclass(frozen=True, slots=True, kw_only=True)
class DocumentContentInput:
    """Explicit text only; the system never writes a diagnosis or instructions."""

    body: str
    recipient: str = ""
    exam_codes: tuple[str, ...] = ()
    cid10_code: str = ""
    cid10_source_version: str = ""

    def payload(self, kind: str) -> DocumentContent:
        """Parse bounded content and refuse uninstalled licensed terminology."""
        if (
            not isinstance(self.body, str)
            or not self.body.strip()
            or len(self.body) > MAX_DOCUMENT_BODY
            or not isinstance(self.recipient, str)
            or len(self.recipient) > MAX_RECIPIENT
            or any(
                ord(character) < CONTROL_FLOOR and character not in "\n\r\t"
                for character in self.body + self.recipient
            )
            or "\x7f" in self.body + self.recipient
            or not isinstance(self.cid10_code, str)
            or not isinstance(self.cid10_source_version, str)
            or not isinstance(self.exam_codes, tuple)
            or any(not isinstance(code, str) for code in self.exam_codes)
        ):
            raise ValidationError(
                _("Check the clinical document fields."), code="invalid_document"
            )
        # No licensed CID dataset or EG-13 decision has been installed. Do not
        # accept a caller's claimed source/version as licensing authority.
        if self.cid10_code or self.cid10_source_version:
            raise ValidationError(
                _("CID-10 is unavailable until a versioned source passes EG-13."),
                code="cid10_unavailable",
            )
        if kind == DocumentKind.EXAM_REQUEST:
            if (
                not self.exam_codes
                or len(set(self.exam_codes)) != len(self.exam_codes)
                or any(code not in CATALOG for code in self.exam_codes)
            ):
                raise ValidationError(
                    _("Choose examinations from the current catalog."),
                    code="invalid_exam_codes",
                )
        elif self.exam_codes:
            raise ValidationError(
                _("Only exam requests may contain examinations."),
                code="invalid_exam_codes",
            )
        return {
            "body": self.body,
            "recipient": self.recipient,
            "exam_codes": list(self.exam_codes),
            "exam_catalog_version": 1 if self.exam_codes else None,
            "cid10_code": "",
            "cid10_source_version": "",
            "cid10_source_record": CID10_SOURCE_RECORD,
        }


def _enrollment(
    encounter: Encounter, *, permission: str = "prescription.prepare"
) -> PatientClinicEnrollment:
    enrollment = PatientClinicEnrollment.objects.filter(
        clinic_id=encounter.clinic_id, patient_id=encounter.patient_id
    ).first()
    if enrollment is None:
        raise ClinicalAccessDeniedError
    require_permission(
        permission,
        clinic_id=encounter.clinic_id,
        patient_enrollment_id=enrollment.pk,
    )
    return enrollment


def document_content(draft: PrescriptionDraft) -> DocumentContent:
    """Parse the retained envelope before it enters a frozen artifact."""
    snapshot = DocumentDraftContent.objects.filter(
        draft=draft, version=draft.version
    ).first()
    if snapshot is None:
        reason = "document_content_required"
        raise ClinicalConflictError(reason)
    raw = snapshot.content
    if not isinstance(raw, dict) or set(raw) != DocumentContent.__required_keys__:
        raise DocumentRenderError
    raw_codes = raw.get("exam_codes")
    if not isinstance(raw_codes, list) or any(
        not isinstance(code, str) for code in raw_codes
    ):
        raise DocumentRenderError
    parsed = DocumentContentInput(
        body=raw["body"],
        recipient=raw["recipient"],
        exam_codes=tuple(raw_codes),
        cid10_code=raw["cid10_code"],
        cid10_source_version=raw["cid10_source_version"],
    ).payload(draft.kind)
    if raw != parsed or (
        raw_codes and type(raw.get("exam_catalog_version")) is not int
    ):
        raise DocumentRenderError
    return parsed


def exam_order_command(draft: PrescriptionDraft) -> UUID:
    """Name the exact approved draft version, never mutable content."""
    return uuid5(draft.pk, f"exam-request:{draft.version}")


def document_order(document: PrescriptionDocument) -> Order | None:
    """Place the approved frozen examination terms inside verified issuance."""
    frozen = document.frozen_input
    if frozen.get("kind") != DocumentKind.EXAM_REQUEST:
        return None
    content = frozen.get("content")
    if (
        not isinstance(content, dict)
        or type(content.get("exam_catalog_version")) is not int
        or content["exam_catalog_version"] != 1
        or not isinstance(content.get("exam_codes"), list)
        or any(not isinstance(code, str) for code in content["exam_codes"])
    ):
        raise DocumentRenderError
    command_id = uuid5(document.draft_id, f"exam-request:{document.document_version}")
    if frozen.get("order_command_id") != str(command_id):
        raise DocumentRenderError
    enrollment = _enrollment(document.encounter)
    return place_order(
        clinic_id=document.clinic_id,
        enrollment_id=enrollment.pk,
        exam_codes=tuple(content["exam_codes"]),
        command_id=command_id,
    )
