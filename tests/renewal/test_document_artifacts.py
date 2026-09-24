"""Immutable PDF/QR artifact acceptance against real clinic_app RLS."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from hashlib import sha256
from html.parser import HTMLParser
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, TypedDict
from uuid import UUID, uuid4

import pytest
import qrcode
import rfc8785
from apps.audit.models import AuditEvent
from apps.ehr.services import (
    ClinicalAccessDeniedError,
    ClinicalConflictError,
    open_encounter,
)
from apps.identity.current_context import CurrentActorError
from apps.identity.models import User, UserClinicRole
from apps.prescription.document_rendering import (
    QR_BORDER,
    QR_MODULE_PT,
    DocumentRenderError,
    RenderedDocument,
    RenderingUnavailableError,
    SyntheticPdfRenderer,
    validate_pdf_structure,
)
from apps.prescription.models import PrescriptionDocument
from apps.prescription.policy import SYNTHETIC_CATEGORY
from apps.prescription.services import (
    create_draft,
    discard_draft,
    download_document,
    render_document,
    save_draft,
    verify_document_integrity,
)
from apps.tenancy.db import TenantAccessDeniedError, tenant_context
from django.db import DatabaseError, connection, transaction
from PIL import Image

from appointment_service_support import (
    create_synthetic_appointment,
    seed_appointment_setup,
)
from patient_service_support import runtime_role
from renewal.test_encounters import physician_client, setup_context

if TYPE_CHECKING:
    from collections.abc import Mapping

    from django.test import Client
    from django.test.client import _MonkeyPatchedWSGIResponse

    from conftest import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)
ITEM = {
    "medication_description": " Medicamento fictício — não utilizar ",
    "strength_form": "Concentração e forma sintéticas",
    "dose": " Dose digitada pelo médico  ",
    "route": "Via sintética",
    "frequency": "Frequência sintética",
    "duration": "Duração sintética",
    "quantity": "Quantidade sintética",
    "instructions": "Texto explícito; sem cálculo automático.",
}


class Scope(TypedDict):
    clinic_id: UUID
    encounter_id: UUID
    patient_id: UUID
    issuer_id: UUID


def seed(graph: RbacGraph) -> Scope:
    setup = seed_appointment_setup(graph)
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_a):
        appointment = create_synthetic_appointment(setup)
    with runtime_role(), tenant_context(graph.physician, graph.organization_a):
        encounter = open_encounter(
            clinic_id=graph.clinic_a, appointment_id=appointment.pk
        )
    return Scope(
        clinic_id=graph.clinic_a,
        encounter_id=encounter.pk,
        patient_id=encounter.patient_id,
        issuer_id=graph.physician,
    )


def seed_rendered(graph: RbacGraph) -> tuple[Scope, PrescriptionDocument]:
    scope = seed(graph)
    with (
        runtime_role(),
        tenant_context(graph.physician, graph.organization_a),
    ):
        draft = create_draft(**scope, category=SYNTHETIC_CATEGORY)
        save_draft(
            **scope,
            draft_id=draft.pk,
            category=SYNTHETIC_CATEGORY,
            expected_version=1,
            items=[ITEM],
        )
        document = render_document(
            clinic_id=scope["clinic_id"],
            draft_id=draft.pk,
            encounter_id=scope["encounter_id"],
            patient_id=scope["patient_id"],
            issuer_id=scope["issuer_id"],
            expected_version=2,
        )
    return scope, document


def _pdf_text(pdf: bytes) -> str:
    """Extract the literal text operands the synthetic profile emitted."""
    return "\n".join(
        match.group(1)
        .decode("cp1252")
        .replace("\\(", "(")
        .replace("\\)", ")")
        .replace("\\\\", "\\")
        for match in re.finditer(rb"\((.*?)\) Tj", pdf)
    )


def _qr_cells(pdf: bytes) -> set[tuple[int, int]]:
    """Reconstruct the drawn QR module set from the content stream."""
    return {
        (int(match.group(1)), int(match.group(2)))
        for match in re.finditer(
            rb"(\d+) (\d+) %d %d re f" % (QR_MODULE_PT, QR_MODULE_PT), pdf
        )
    }


def _expected_qr_cells(payload: str) -> set[tuple[int, int]]:
    """Encode the payload and return module coordinates from the origin."""
    code = qrcode.QRCode(border=4, box_size=1)
    code.add_data(payload)
    code.make(fit=True)
    matrix = code.get_matrix()
    size = len(matrix)
    cells = {
        (column, size - 1 - row)
        for row, row_cells in enumerate(matrix)
        for column, dark in enumerate(row_cells)
        if dark
    }
    min_x = min(x for x, _ in cells)
    min_y = min(y for _, y in cells)
    return {(x - min_x, y - min_y) for x, y in cells}


def _normalized_cells(cells: set[tuple[int, int]]) -> set[tuple[int, int]]:
    """Convert drawn point offsets into module coordinates from the origin."""
    min_x = min(x for x, _ in cells)
    min_y = min(y for _, y in cells)
    return {
        ((x - min_x) // QR_MODULE_PT, (y - min_y) // QR_MODULE_PT) for x, y in cells
    }


def test_render_binds_version_params_digests_and_visible_fields(
    rbac_graph: RbacGraph,
) -> None:
    scope, document = seed_rendered(rbac_graph)
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        stored = PrescriptionDocument.objects.get(pk=document.pk)
        assert stored.document_version == 2
        assert stored.state == "rendered"
        assert stored.draft_id is not None
        assert stored.encounter_id == scope["encounter_id"]
        assert stored.patient_id == scope["patient_id"]
        assert stored.issuer_id == scope["issuer_id"]
        assert stored.render_params["renderer"] == "synthetic-pdf-v1"
        assert stored.render_params["verification_url"].endswith(stored.qr_handle)
        frozen = stored.frozen_input
        assert frozen["document_version"] == 2
        assert frozen["items"] == [ITEM]
        assert frozen["input_digest"] == stored.input_digest
        canonical = {k: v for k, v in frozen.items() if k != "input_digest"}
        assert sha256(rfc8785.dumps(canonical)).hexdigest() == stored.input_digest
        pdf = bytes(stored.pdf_bytes)
        assert pdf.startswith(b"%PDF-")
        assert pdf.rstrip().endswith(b"%%EOF")
        assert sha256(pdf).hexdigest() == stored.pdf_digest
        text = _pdf_text(pdf)
        flat = text.replace("\n", "")
        for required in (
            frozen["issuer_label"],
            frozen["patient_label"],
            frozen["clinic_label"],
            frozen["issued_at"],
            ITEM["dose"].strip(),
        ):
            assert required in text
        # Long values wrap at the glyph-measured width and survive intact.
        assert frozen["verification_url"] in flat
        assert stored.input_digest in flat
        # The QR encodes exactly the public verification URL, nothing else.
        drawn = _normalized_cells(_qr_cells(pdf))
        assert drawn == _expected_qr_cells(frozen["verification_url"])
        assert stored.qr_handle in frozen["verification_url"]
        for forbidden in (
            str(scope["patient_id"]),
            str(scope["encounter_id"]),
            str(scope["issuer_id"]),
            str(stored.draft_id),
        ):
            assert forbidden not in frozen["verification_url"]
            assert forbidden not in text
        integrity = verify_document_integrity(stored)
        assert integrity.ok
        assert integrity.reason_code == "verified"
    with setup_context(rbac_graph.organization_a):
        events = list(
            AuditEvent.objects.filter(
                event_type__startswith="prescription.document."
            ).values_list("event_type", "payload")
        )
        assert [event for event, _ in events] == ["prescription.document.rendered"]
        assert all(
            set(payload) == {"clinic_id", "object_verb"} for _, payload in events
        )


def test_identical_frozen_input_renders_identical_bytes(
    rbac_graph: RbacGraph,
) -> None:
    _scope, document = seed_rendered(rbac_graph)
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        stored = PrescriptionDocument.objects.get(pk=document.pk)
        rerendered = SyntheticPdfRenderer().render(stored.frozen_input)
        assert rerendered.pdf_bytes == bytes(stored.pdf_bytes)
        assert rerendered.pdf_digest == stored.pdf_digest


def test_authorized_download_and_denials(rbac_graph: RbacGraph) -> None:
    scope, document = seed_rendered(rbac_graph)
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        download = download_document(
            clinic_id=scope["clinic_id"], document_id=document.pk
        )
        assert download.content_type == "application/pdf"
        assert sha256(download.data).hexdigest() == document.pdf_digest
        assert download.data == bytes(
            PrescriptionDocument.objects.get(pk=document.pk).pdf_bytes
        )
        # The QR handle is not download authority and never resolves a record.
        with pytest.raises((ClinicalAccessDeniedError, ValueError)):
            download_document(
                clinic_id=scope["clinic_id"],
                document_id=UUID(document.qr_handle.ljust(36, "0")[:36]),
            )
    # A same-clinic non-physician cannot read or download the artifact.
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        assert not PrescriptionDocument.objects.exists()
        with pytest.raises((ClinicalAccessDeniedError, CurrentActorError)):
            download_document(clinic_id=scope["clinic_id"], document_id=document.pk)
    # Another tenant cannot see the artifact at all.
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_b),
    ):
        assert not PrescriptionDocument.objects.exists()
        with pytest.raises(ClinicalAccessDeniedError):
            download_document(clinic_id=scope["clinic_id"], document_id=document.pk)
    with setup_context(rbac_graph.organization_a):
        verbs = set(
            AuditEvent.objects.filter(
                event_type="prescription.document.downloaded"
            ).values_list("payload__object_verb", flat=True)
        )
        assert verbs == {"downloaded"}


def test_alter_after_freeze_keeps_original_artifact(rbac_graph: RbacGraph) -> None:
    scope, document = seed_rendered(rbac_graph)
    original_pdf = bytes(document.pdf_bytes)
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        draft = document.draft
        # Re-rendering the same frozen version is refused.
        with pytest.raises(ClinicalConflictError, match="already_rendered"):
            render_document(
                clinic_id=scope["clinic_id"],
                draft_id=draft.pk,
                encounter_id=scope["encounter_id"],
                patient_id=scope["patient_id"],
                issuer_id=scope["issuer_id"],
                expected_version=2,
            )
        # Altering the draft after freeze creates a new version; the stored
        # artifact still binds the frozen version and its original bytes.
        save_draft(
            **scope,
            draft_id=draft.pk,
            category=SYNTHETIC_CATEGORY,
            expected_version=2,
            items=[{**ITEM, "dose": "Dose alterada após congelamento"}],
        )
        stored = PrescriptionDocument.objects.get(pk=document.pk)
        assert stored.document_version == 2
        assert bytes(stored.pdf_bytes) == original_pdf
        assert stored.frozen_input["items"] == [ITEM]
        assert verify_document_integrity(stored).ok
        # Direct mutation of stored bytes, digests or handle is rejected.
        for fields in (
            {"pdf_bytes": b"%PDF-1.4 forged"},
            {"pdf_digest": "0" * 64},
            {"qr_handle": "a" * 43},
            {"document_version": 3},
        ):
            with pytest.raises(DatabaseError), transaction.atomic():
                PrescriptionDocument.objects.filter(pk=document.pk).update(**fields)
        with pytest.raises(DatabaseError), transaction.atomic():
            PrescriptionDocument.objects.filter(pk=document.pk).delete()
        assert (
            bytes(PrescriptionDocument.objects.get(pk=document.pk).pdf_bytes)
            == original_pdf
        )


def test_tampered_stored_content_fails_integrity(rbac_graph: RbacGraph) -> None:
    _scope, document = seed_rendered(rbac_graph)
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        stored = PrescriptionDocument.objects.get(pk=document.pk)
        stored.pdf_bytes = b"%PDF-1.4 tampered"
        verdict = verify_document_integrity(stored)
        assert not verdict.ok
        assert verdict.reason_code == "pdf_digest_mismatch"
        stored = PrescriptionDocument.objects.get(pk=document.pk)
        altered_input = dict(stored.frozen_input)
        altered_input["patient_label"] = "Paciente Alterado"
        stored.frozen_input = altered_input
        verdict = verify_document_integrity(stored)
        assert not verdict.ok
        assert verdict.reason_code == "input_digest_mismatch"
        stored = PrescriptionDocument.objects.get(pk=document.pk)
        assert verify_document_integrity(stored).ok


def test_malformed_renderer_output_and_unavailability_block_artifact(
    rbac_graph: RbacGraph,
) -> None:
    scope = seed(rbac_graph)

    class MalformedRenderer:
        renderer_id = "malformed"

        def render(self, frozen_input: Mapping[str, object]) -> RenderedDocument:
            data = b"not a pdf at all"
            return RenderedDocument(pdf_bytes=data, pdf_digest=sha256(data).hexdigest())

    class MarkerWrappedRenderer:
        renderer_id = "malformed-with-markers"

        def render(self, frozen_input: Mapping[str, object]) -> RenderedDocument:
            # Magic bytes and %%EOF alone are not a PDF: no xref, no trailer.
            data = b"%PDF-1.4\nnot a PDF\n%%EOF\n"
            return RenderedDocument(pdf_bytes=data, pdf_digest=sha256(data).hexdigest())

    class BadXrefRenderer:
        renderer_id = "malformed-xref"

        def render(self, frozen_input: Mapping[str, object]) -> RenderedDocument:
            # startxref points at the trailer, not at an xref table.
            data = (
                b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n"
                b"trailer\n<< /Size 2 /Root 1 0 R >>\n"
                b"startxref\n30\n%%EOF\n"
            )
            return RenderedDocument(pdf_bytes=data, pdf_digest=sha256(data).hexdigest())

    class WrongDigestRenderer:
        renderer_id = "wrong-digest"

        def render(self, frozen_input: Mapping[str, object]) -> RenderedDocument:
            rendered = SyntheticPdfRenderer().render(frozen_input)
            return RenderedDocument(pdf_bytes=rendered.pdf_bytes, pdf_digest="0" * 64)

    class UnavailableRenderer:
        renderer_id = "unavailable"

        def render(self, frozen_input: Mapping[str, object]) -> RenderedDocument:
            raise RenderingUnavailableError

    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        draft = create_draft(**scope, category=SYNTHETIC_CATEGORY)
        save_draft(
            **scope,
            draft_id=draft.pk,
            category=SYNTHETIC_CATEGORY,
            expected_version=1,
            items=[ITEM],
        )
        for engine in (
            MalformedRenderer(),
            MarkerWrappedRenderer(),
            BadXrefRenderer(),
            WrongDigestRenderer(),
            UnavailableRenderer(),
        ):
            with (
                pytest.raises((DocumentRenderError, RenderingUnavailableError)),
                transaction.atomic(),
            ):
                render_document(
                    clinic_id=scope["clinic_id"],
                    draft_id=draft.pk,
                    encounter_id=scope["encounter_id"],
                    patient_id=scope["patient_id"],
                    issuer_id=scope["issuer_id"],
                    expected_version=2,
                    renderer=engine,
                )
        assert not PrescriptionDocument.objects.exists()
        # The failed attempts leave the draft renderable afterwards.
        document = render_document(
            clinic_id=scope["clinic_id"],
            draft_id=draft.pk,
            encounter_id=scope["encounter_id"],
            patient_id=scope["patient_id"],
            issuer_id=scope["issuer_id"],
            expected_version=2,
        )
        assert document.state == "rendered"
        assert verify_document_integrity(document).ok


def _xref_pdf(
    objects: list[bytes],
    trailer_entries: bytes = b"",
    *,
    pages_generation: int = 0,
    header_separator: bytes = b" ",
) -> bytes:
    """Build independent test envelopes with offsets computed from actual bytes."""
    pdf = bytearray(b"%PDF-1.4\n")
    entries = [b"0000000000 65535 f \n"]
    for number, body in enumerate(objects, 1):
        generation = pages_generation if number == 2 else 0
        entries.append(f"{len(pdf):010d} {generation:05d} n \n".encode())
        pdf += (
            str(number).encode()
            + header_separator
            + str(generation).encode()
            + header_separator
            + b"obj\n"
            + body
            + b"\nendobj\n"
        )
    xref_at = len(pdf)
    pdf += f"xref\n0 {len(entries)}\n".encode() + b"".join(entries)
    pdf += (
        f"trailer\n<< /Size {len(entries)} /Root 1 0 R".encode()
        + trailer_entries
        + f" >>\nstartxref\n{xref_at}\n%%EOF\n".encode()
    )
    return bytes(pdf)


def _profile_objects(text: bytes = b"Supported profile") -> list[bytes]:
    stream = b"BT /F1 11 Tf 56 800 Td 14 TL\nT* (" + text + b") Tj\nET\n"
    return [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length "
        + str(len(stream)).encode()
        + b" >>\nstream\n"
        + stream
        + b"endstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
        b"/Encoding /WinAnsiEncoding >>",
    ]


def _two_page_profile_objects() -> list[bytes]:
    objects = _profile_objects()
    objects[1] = b"<< /Type /Pages /Kids [3 0 R 6 0 R] /Count 2 >>"
    return [
        *objects,
        objects[2].replace(b"/Contents 4 0 R", b"/Contents 7 0 R"),
        objects[3],
    ]


def _lexical_profile_pdfs() -> list[tuple[str, bytes]]:
    objects = _two_page_profile_objects()
    control = _xref_pdf(objects)
    cases = [
        ("vertical-tab-header", _xref_pdf(objects, header_separator=b"\x0b")),
        ("vertical-tab-body", control.replace(b"obj\n<<", b"obj\x0b<<")),
        ("vertical-tab-xref", control.replace(b"xref\n0 ", b"xref\x0b0 ")),
        ("vertical-tab-subsection", control.replace(b"0 8\n", b"0 8\x0b\n")),
        ("vertical-tab-startxref", control.replace(b"startxref\n", b"startxref\x0b")),
        ("vertical-tab-eof", control + b"\x0b"),
    ]
    for name, replacement in (
        ("joined-reference-digit", b"R6"),
        ("joined-reference-letter", b"Rword 6"),
        ("joined-reference-sign", b"R+6"),
        ("vertical-tab-reference", b"R\x0b6"),
    ):
        mutated = objects.copy()
        mutated[1] = mutated[1].replace(b"R 6", replacement)
        cases.append((name, _xref_pdf(mutated)))
    return cases


@pytest.mark.parametrize("separator", [b"\x00", b"\t", b"\n", b"\f", b"\r", b" "])
def test_pdf_whitespace_keeps_multipage_content_readable(separator: bytes) -> None:
    objects = _two_page_profile_objects()
    # Exercise both header fields and reference components with every PDF
    # whitespace byte, including NUL (which Python's regex \\s omits).
    objects = [
        body.replace(b" 0 R", separator + b"0" + separator + b"R") for body in objects
    ]
    pdf = _xref_pdf(objects, header_separator=separator)
    validate_pdf_structure(pdf)
    result = subprocess.run(  # noqa: S603 - fixed poppler tool and fixture bytes
        [_poppler("pdftotext"), "-", "-"],
        input=pdf,
        capture_output=True,
        check=True,
        timeout=15,
    )
    assert not result.stderr
    pages = result.stdout.split(b"\f")
    assert len(pages) == 3
    assert not pages[-1]
    assert all(b"Supported profile" in page for page in pages[:-1])


def test_reference_delimiters_keep_multipage_content_readable() -> None:
    objects = _two_page_profile_objects()
    objects = [body.replace(b"R >>", b"R>>").replace(b"R /", b"R/") for body in objects]
    objects[1] = objects[1].replace(b"R 6", b"R% reference separator\n6")
    pdf = _xref_pdf(objects)
    validate_pdf_structure(pdf)
    result = subprocess.run(  # noqa: S603 - fixed poppler tool and fixture bytes
        [_poppler("pdftotext"), "-", "-"],
        input=pdf,
        capture_output=True,
        check=True,
        timeout=15,
    )
    assert not result.stderr
    pages = result.stdout.split(b"\f")
    assert len(pages) == 3
    assert not pages[-1]
    assert all(b"Supported profile" in page for page in pages[:-1])


def _unsupported_profile_pdfs() -> list[tuple[str, bytes]]:
    cases = [
        ("trailer-" + entry.decode(), _xref_pdf(_profile_objects(), b" " + entry))
        for entry in (
            b"/Encrypt 6 0 R",
            b"/Info null",
            b"/ID []",
            b"/Prev 0",
            b"/XRefStm 0",
            b"/Unknown null",
        )
    ]
    # The exact round-8 envelope, plus encryption on an otherwise complete
    # supported graph so rejection cannot depend on missing page resources.
    round8 = _xref_pdf(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] >>",
            b"<< /Filter 42 >>",
        ],
        b" /Encrypt 4 0 R",
    )
    assert sha256(round8).hexdigest() == (
        "b27a859f404d9a438754b07ab91bd6a2424f1643be137dd6171a745fd8dcab56"
    )
    cases.append(("round8-exact", round8))
    cases.append(
        (
            "round8-complete-graph",
            _xref_pdf([*_profile_objects(), b"<< /Filter 42 >>"], b" /Encrypt 6 0 R"),
        )
    )
    mutations = [
        ("catalog-extra", 0, b" >>", b" /OpenAction null >>"),
        ("catalog-note", 0, b" >>", b" /Note (see /Pages 9 9 R) >>"),
        ("catalog-escaped-extra", 0, b" >>", b" /No#23te null >>"),
        ("pages-extra", 1, b" >>", b" /Rotate 42 >>"),
        ("page-extra", 2, b"/Parent", b"/Annots null /Parent"),
        ("resources-extra", 2, b"/Font <<", b"/XObject null /Font <<"),
        ("font-map-extra", 2, b"/F1", b"/F2 null /F1"),
        ("font-extra", 4, b" >>", b" /ToUnicode null >>"),
        ("font-subtype", 4, b"/Type1", b"/Type3"),
        ("font-base", 4, b"/Helvetica", b"/Unsupported"),
        ("font-encoding", 4, b"/WinAnsiEncoding", b"42"),
        ("stream-filter-number", 3, b" >>", b" /Filter 42 >>"),
        ("stream-filter-flate", 3, b" >>", b" /Filter /FlateDecode >>"),
        ("stream-filter-null", 3, b" >>", b" /Filter null >>"),
        ("stream-filter-array", 3, b" >>", b" /Filter [] >>"),
        ("stream-extra", 3, b" >>", b" /DecodeParms << >> >>"),
        ("stream-external", 3, b" >>", b" /F (external) >>"),
        ("stream-length-missing", 3, b"/Length", b"/Ignored"),
        ("page-contents-missing", 2, b"/Contents 4 0 R", b""),
        ("page-contents-dangling", 2, b"/Contents 4 0 R", b"/Contents 9 0 R"),
        ("page-font-dangling", 2, b"/F1 5 0 R", b"/F1 5 1 R"),
        ("page-parent", 2, b"/Parent 2 0 R", b"/Parent 1 0 R"),
        ("page-box-type", 2, b"[0 0 595 842]", b"[false 0 595 842]"),
        ("page-box-shape", 2, b"[0 0 595 842]", b"[0 0 0 0]"),
        ("page-type", 2, b"/Type /Page", b"/Type /Unknown"),
        ("duplicate-kid", 1, b"[3 0 R] /Count 1", b"[3 0 R 3 0 R] /Count 2"),
        ("stream-operator", 3, b" Tj", b" XX"),
        ("stream-truncated", 3, b"endstream", b"endstrea"),
        ("object-trailing-value", 4, b" >>", b" >> null"),
    ]
    for name, index, old, new in mutations:
        objects = _profile_objects()
        assert old in objects[index]
        objects[index] = objects[index].replace(old, new, 1)
        cases.append((name, _xref_pdf(objects)))
    for length in (b"0", b"99999", b"-1", b"true", b"4 0 R"):
        objects = _profile_objects()
        objects[3] = re.sub(rb"/Length \d+", b"/Length " + length, objects[3])
        cases.append(("stream-length-" + length.decode(), _xref_pdf(objects)))
    cases.extend(
        ("extra-object-" + extra.decode(), _xref_pdf([*_profile_objects(), extra]))
        for extra in (
            b"<< /Type /Unknown >>",
            b"<< /Filter 42 >>",
            b"42",
            _profile_objects()[-1],
        )
    )
    return cases


@pytest.mark.parametrize(
    ("case", "pdf"),
    _unsupported_profile_pdfs() + _lexical_profile_pdfs(),
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_unsupported_profile_rejected_before_storage(
    rbac_graph: RbacGraph,
    case: str,
    pdf: bytes,
) -> None:
    with pytest.raises(DocumentRenderError):
        validate_pdf_structure(pdf)
    scope, original = seed_rendered(rbac_graph)

    class UnsupportedRenderer:
        renderer_id = "unsupported-profile"

        def render(self, frozen_input: Mapping[str, object]) -> RenderedDocument:
            return RenderedDocument(pdf_bytes=pdf, pdf_digest=sha256(pdf).hexdigest())

    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        save_draft(
            **scope,
            draft_id=original.draft_id,
            category=SYNTHETIC_CATEGORY,
            expected_version=2,
            items=[ITEM],
        )
        with pytest.raises(DocumentRenderError):
            render_document(
                **scope,
                draft_id=original.draft_id,
                expected_version=3,
                renderer=UnsupportedRenderer(),
            )
        stored = PrescriptionDocument.objects.get()
        assert stored.pk == original.pk
        for field in (
            "pdf_digest",
            "input_digest",
            "qr_handle",
            "document_version",
            "frozen_input",
            "render_params",
        ):
            assert getattr(stored, field) == getattr(original, field)
        assert bytes(stored.pdf_bytes) == bytes(original.pdf_bytes)
        assert download_document(
            clinic_id=scope["clinic_id"],
            document_id=original.pk,
        ).data == bytes(original.pdf_bytes)
        assert verify_document_integrity(stored).ok


def test_supported_profile_names_generations_and_opaque_text() -> None:
    objects = _profile_objects(b"see /Pages 9 9 R and /No#23te")
    validate_pdf_structure(_xref_pdf(objects))
    objects[0] = objects[0].replace(b"/Pages", b"/Pa#67es")
    objects[1] = objects[1].replace(b"/Pages", b"/Pa#67es")
    validate_pdf_structure(_xref_pdf(objects))
    objects = [body.replace(b"2 0 R", b"2 1 R") for body in objects]
    pdf = _xref_pdf(objects, pages_generation=1)
    validate_pdf_structure(pdf)
    # An independent reader must also open the supported positive control.
    result = subprocess.run(  # noqa: S603 - fixed poppler tool and fixture bytes
        [_poppler("pdftotext"), "-", "-"],
        input=pdf,
        capture_output=True,
        check=True,
    )
    assert b"see /Pages 9 9 R and /No#23te" in result.stdout


def test_broken_object_graph_renderer_output_is_rejected(
    rbac_graph: RbacGraph,
) -> None:
    """A valid xref/trailer envelope without a usable page tree is refused.

    Regression for T33-MALFORMED: each fixture parses as an xref-table PDF
    whose trailer resolves to a catalog, but the object graph has no
    readable page tree — no ``/Pages`` entry, an empty ``/Kids`` array, a
    kid that is not a ``/Type /Page`` object, a ``/Pages`` reference
    whose generation does not match any existing object, a ``/Count``
    that is missing or inconsistent with the kids, a ``/Pages`` entry
    shadowed by an escaped or duplicated key, a malformed ``#`` name
    escape, a ``#`` escape decoding to a byte that is not a valid name
    character, or a ``/Pages`` look-alike that exists only inside a
    literal or hex string, a comment, a value position or a nested
    dictionary.
    Poppler rejects these or — for escapes decoding to bytes that are
    not name characters — reads a different dictionary than a strict
    reader; the rendering boundary must reject them before insertion and
    keep the original artifact untouched.
    """
    scope, original = seed_rendered(rbac_graph)
    original_pdf = bytes(original.pdf_bytes)

    class BrokenGraphRenderer:
        renderer_id = "malformed-broken-object-graph"

        def __init__(self, pdf: bytes) -> None:
            self.pdf = pdf

        def render(self, frozen_input: Mapping[str, object]) -> RenderedDocument:
            return RenderedDocument(
                pdf_bytes=self.pdf, pdf_digest=sha256(self.pdf).hexdigest()
            )

    fixtures = [
        # Catalog without a /Pages entry (the round-2 gate reproduction).
        (
            b"%PDF-1.4\n"
            b"1 0 obj\n<< /Type /Catalog >>\nendobj\n"
            b"xref\n0 2\n"
            b"0000000000 65535 f \n"
            b"0000000009 00000 n \n"
            b"trailer\n<< /Size 2 /Root 1 0 R >>\n"
            b"startxref\n45\n%%EOF\n"
        ),
        # /Pages resolves, but its /Kids array is empty.
        (
            b"%PDF-1.4\n"
            b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
            b"2 0 obj\n<< /Type /Pages /Kids [] /Count 0 >>\nendobj\n"
            b"xref\n0 3\n"
            b"0000000000 65535 f \n"
            b"0000000009 00000 n \n"
            b"0000000058 00000 n \n"
            b"trailer\n<< /Size 3 /Root 1 0 R >>\n"
            b"startxref\n110\n%%EOF\n"
        ),
        # /Kids points at an object that is not a /Type /Page.
        (
            b"%PDF-1.4\n"
            b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
            b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
            b"3 0 obj\n<< /Type /Font >>\nendobj\n"
            b"xref\n0 4\n"
            b"0000000000 65535 f \n"
            b"0000000009 00000 n \n"
            b"0000000058 00000 n \n"
            b"0000000115 00000 n \n"
            b"trailer\n<< /Size 4 /Root 1 0 R >>\n"
            b"startxref\n148\n%%EOF\n"
        ),
        # /Pages 2 1 R but only 2 0 obj exists: the generation is part of
        # the reference identity, so this is a dangling reference, not a
        # resolution to generation zero (the round-3 gate reproduction,
        # SHA-256 92829e00…; poppler exits 99 on it).
        (
            b"%PDF-1.4\n"
            b"1 0 obj\n<< /Type /Catalog /Pages 2 1 R >>\nendobj\n"
            b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
            b"3 0 obj\n<< /Type /Page /Parent 2 0 R "
            b"/MediaBox [0 0 595 842] >>\nendobj\n"
            b"xref\n0 4\n"
            b"0000000000 65535 f \n"
            b"0000000009 00000 n \n"
            b"0000000058 00000 n \n"
            b"0000000115 00000 n \n"
            b"trailer\n<< /Size 4 /Root 1 0 R >>\n"
            b"startxref\n186\n%%EOF\n"
        ),
        # /Pages 2 0 R appears only inside a literal string: the catalog
        # has a /Note entry, not a /Pages entry (the round-4 gate
        # reproduction, SHA-256 1f96ab49…; poppler exits 99 on it).
        (
            b"%PDF-1.4\n"
            b"1 0 obj\n<< /Type /Catalog /Note (/Pages 2 0 R) >>\nendobj\n"
            b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
            b"3 0 obj\n<< /Type /Page /Parent 2 0 R "
            b"/MediaBox [0 0 595 842] >>\nendobj\n"
            b"xref\n0 4\n"
            b"0000000000 65535 f \n"
            b"0000000009 00000 n \n"
            b"0000000066 00000 n \n"
            b"0000000123 00000 n \n"
            b"trailer\n<< /Size 4 /Root 1 0 R >>\n"
            b"startxref\n194\n%%EOF\n"
        ),
        # Same look-alike inside a hex string: <2F...> decodes to
        # "/Pages 2 0 R" but is still string content, not a dictionary
        # entry.
        (
            b"%PDF-1.4\n"
            b"1 0 obj\n<< /Type /Catalog "
            b"/Note <2F5061676573203220302052> >>\nendobj\n"
            b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
            b"3 0 obj\n<< /Type /Page /Parent 2 0 R "
            b"/MediaBox [0 0 595 842] >>\nendobj\n"
            b"xref\n0 4\n"
            b"0000000000 65535 f \n"
            b"0000000009 00000 n \n"
            b"0000000078 00000 n \n"
            b"0000000135 00000 n \n"
            b"trailer\n<< /Size 4 /Root 1 0 R >>\n"
            b"startxref\n206\n%%EOF\n"
        ),
        # The look-alike inside a % end-of-line comment: the catalog has
        # only /Type, no /Pages entry (poppler exits 99).
        (
            b"%PDF-1.4\n"
            b"1 0 obj\n<< /Type /Catalog % /Pages 2 0 R\n>>\nendobj\n"
            b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
            b"3 0 obj\n<< /Type /Page /Parent 2 0 R "
            b"/MediaBox [0 0 595 842] >>\nendobj\n"
            b"xref\n0 4\n"
            b"0000000000 65535 f \n"
            b"0000000009 00000 n \n"
            b"0000000060 00000 n \n"
            b"0000000117 00000 n \n"
            b"trailer\n<< /Size 4 /Root 1 0 R >>\n"
            b"startxref\n188\n%%EOF\n"
        ),
        # /Pages as a name in value position: /Note's value is the name
        # /Pages, so "2 0 R" is dangling content, not a dictionary entry
        # (poppler exits 99: dictionary key must be a name object).
        (
            b"%PDF-1.4\n"
            b"1 0 obj\n<< /Type /Catalog /Note /Pages 2 0 R >>\nendobj\n"
            b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
            b"3 0 obj\n<< /Type /Page /Parent 2 0 R "
            b"/MediaBox [0 0 595 842] >>\nendobj\n"
            b"xref\n0 4\n"
            b"0000000000 65535 f \n"
            b"0000000009 00000 n \n"
            b"0000000064 00000 n \n"
            b"0000000121 00000 n \n"
            b"trailer\n<< /Size 4 /Root 1 0 R >>\n"
            b"startxref\n192\n%%EOF\n"
        ),
        # /Pages inside a nested dictionary: the catalog's own keys are
        # /Type and /X only (poppler exits 99).
        (
            b"%PDF-1.4\n"
            b"1 0 obj\n<< /Type /Catalog /X << /Pages 2 0 R >> >>\nendobj\n"
            b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
            b"3 0 obj\n<< /Type /Page /Parent 2 0 R "
            b"/MediaBox [0 0 595 842] >>\nendobj\n"
            b"xref\n0 4\n"
            b"0000000000 65535 f \n"
            b"0000000009 00000 n \n"
            b"0000000067 00000 n \n"
            b"0000000124 00000 n \n"
            b"trailer\n<< /Size 4 /Root 1 0 R >>\n"
            b"startxref\n195\n%%EOF\n"
        ),
        # /Kids has one page but /Count says 0: readers size the document
        # from /Count, so the tree is unreadable (the round-6 gate
        # reproduction, SHA-256 ebfabe2a…; poppler exits 99: Invalid page
        # count 0).
        (
            b"%PDF-1.4\n"
            b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
            b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 0 >>\nendobj\n"
            b"3 0 obj\n<< /Type /Page /Parent 2 0 R "
            b"/MediaBox [0 0 595 842] >>\nendobj\n"
            b"xref\n0 4\n"
            b"0000000000 65535 f \n"
            b"0000000009 00000 n \n"
            b"0000000058 00000 n \n"
            b"0000000115 00000 n \n"
            b"trailer\n<< /Size 4 /Root 1 0 R >>\n"
            b"startxref\n186\n%%EOF\n"
        ),
        # /Count larger than the actual kids is equally inconsistent
        # (poppler exits 99: page count larger than number of objects).
        (
            b"%PDF-1.4\n"
            b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
            b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 5 >>\nendobj\n"
            b"3 0 obj\n<< /Type /Page /Parent 2 0 R "
            b"/MediaBox [0 0 595 842] >>\nendobj\n"
            b"xref\n0 4\n"
            b"0000000000 65535 f \n"
            b"0000000009 00000 n \n"
            b"0000000058 00000 n \n"
            b"0000000115 00000 n \n"
            b"trailer\n<< /Size 4 /Root 1 0 R >>\n"
            b"startxref\n186\n%%EOF\n"
        ),
        # /Count absent: readers cannot size the page tree (poppler
        # exits 99: page count is wrong type).
        (
            b"%PDF-1.4\n"
            b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
            b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] >>\nendobj\n"
            b"3 0 obj\n<< /Type /Page /Parent 2 0 R "
            b"/MediaBox [0 0 595 842] >>\nendobj\n"
            b"xref\n0 4\n"
            b"0000000000 65535 f \n"
            b"0000000009 00000 n \n"
            b"0000000058 00000 n \n"
            b"0000000106 00000 n \n"
            b"trailer\n<< /Size 4 /Root 1 0 R >>\n"
            b"startxref\n177\n%%EOF\n"
        ),
        # /Pa#67es decodes to /Pages under PDF name semantics: the
        # catalog's real /Pages entry is shadowed by null, so readers see
        # no page tree (the round-6 gate reproduction, SHA-256
        # a6277afd…; poppler exits 99: top-level pages object is null).
        (
            b"%PDF-1.4\n"
            b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R "
            b"/Pa#67es null >>\nendobj\n"
            b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
            b"3 0 obj\n<< /Type /Page /Parent 2 0 R "
            b"/MediaBox [0 0 595 842] >>\nendobj\n"
            b"xref\n0 4\n"
            b"0000000000 65535 f \n"
            b"0000000009 00000 n \n"
            b"0000000072 00000 n \n"
            b"0000000129 00000 n \n"
            b"trailer\n<< /Size 4 /Root 1 0 R >>\n"
            b"startxref\n200\n%%EOF\n"
        ),
        # The same shadowing with a literal duplicate key: readers
        # disagree on which entry wins, so the graph is ambiguous.
        (
            b"%PDF-1.4\n"
            b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R "
            b"/Pages null >>\nendobj\n"
            b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
            b"3 0 obj\n<< /Type /Page /Parent 2 0 R "
            b"/MediaBox [0 0 595 842] >>\nendobj\n"
            b"xref\n0 4\n"
            b"0000000000 65535 f \n"
            b"0000000009 00000 n \n"
            b"0000000070 00000 n \n"
            b"0000000127 00000 n \n"
            b"trailer\n<< /Size 4 /Root 1 0 R >>\n"
            b"startxref\n198\n%%EOF\n"
        ),
        # A # escape without two hex digits is malformed name syntax.
        (
            b"%PDF-1.4\n"
            b"1 0 obj\n<< /Type /Catalog /Pa#6zes 2 0 R >>\nendobj\n"
            b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
            b"3 0 obj\n<< /Type /Page /Parent 2 0 R "
            b"/MediaBox [0 0 595 842] >>\nendobj\n"
            b"xref\n0 4\n"
            b"0000000000 65535 f \n"
            b"0000000009 00000 n \n"
            b"0000000060 00000 n \n"
            b"0000000117 00000 n \n"
            b"trailer\n<< /Size 4 /Root 1 0 R >>\n"
            b"startxref\n188\n%%EOF\n"
        ),
        # /Pages#00 decodes to /Pages<NUL>: NUL is not a valid PDF name
        # character, so readers disagree on the key and the catalog's
        # real /Pages entry is shadowed by null (the round-7 gate
        # reproduction, SHA-256 285944c3…; poppler exits 99: top-level
        # pages object is null).
        (
            b"%PDF-1.4\n"
            b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R "
            b"/Pages#00 null >>\nendobj\n"
            b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
            b"3 0 obj\n<< /Type /Page /Parent 2 0 R "
            b"/MediaBox [0 0 595 842] >>\nendobj\n"
            b"xref\n0 4\n"
            b"0000000000 65535 f \n"
            b"0000000009 00000 n \n"
            b"0000000073 00000 n \n"
            b"0000000130 00000 n \n"
            b"trailer\n<< /Size 4 /Root 1 0 R >>\n"
            b"startxref\n201\n%%EOF\n"
        ),
    ]
    # Other escapes decoding to bytes that are not name characters —
    # whitespace (#20 space, #09 tab) and delimiters (#28 '(', #2f '/',
    # #25 '%') — are refused at the shared name parser for the same
    # reason. Poppler tolerates these as distinct keys (exit 0), but a
    # name that needs them is not one every reader agrees on, so the
    # boundary fails closed.
    null_name = fixtures[-1]
    fixtures.extend(
        null_name.replace(b"#00", escape)
        for escape in (b"#20", b"#09", b"#28", b"#2f", b"#25")
    )
    for pdf in fixtures:
        with pytest.raises(DocumentRenderError):
            validate_pdf_structure(pdf)
    # Supported controls now include the mandatory content/resources/font;
    # see test_supported_profile_names_generations_and_opaque_text. A catalog
    # /Note is an unsupported key, even when its string is harmless PDF text.
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        draft = original.draft
        for pdf in fixtures:
            draft = save_draft(
                **scope,
                draft_id=draft.pk,
                category=SYNTHETIC_CATEGORY,
                expected_version=draft.version,
                items=[ITEM],
            )
            with pytest.raises(DocumentRenderError), transaction.atomic():
                render_document(
                    clinic_id=scope["clinic_id"],
                    draft_id=draft.pk,
                    encounter_id=scope["encounter_id"],
                    patient_id=scope["patient_id"],
                    issuer_id=scope["issuer_id"],
                    expected_version=draft.version,
                    renderer=BrokenGraphRenderer(pdf),
                )
        # No new artifact exists; the original survives byte-for-byte.
        documents = list(PrescriptionDocument.objects.all())
        assert [document.pk for document in documents] == [original.pk]
        stored = documents[0]
        assert bytes(stored.pdf_bytes) == original_pdf
        assert stored.pdf_digest == original.pdf_digest
        assert stored.input_digest == original.input_digest
        assert stored.qr_handle == original.qr_handle
        assert stored.document_version == 2
        assert verify_document_integrity(stored).ok


def test_render_requires_live_draft_and_exact_version(
    rbac_graph: RbacGraph,
) -> None:
    scope = seed(rbac_graph)
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        draft = create_draft(**scope, category=SYNTHETIC_CATEGORY)
        with pytest.raises(ClinicalConflictError, match="stale_revision"):
            render_document(
                clinic_id=scope["clinic_id"],
                draft_id=draft.pk,
                encounter_id=scope["encounter_id"],
                patient_id=scope["patient_id"],
                issuer_id=scope["issuer_id"],
                expected_version=7,
            )
        discard_draft(**scope, draft_id=draft.pk, expected_version=1)
        with pytest.raises(ClinicalAccessDeniedError):
            render_document(
                clinic_id=scope["clinic_id"],
                draft_id=draft.pk,
                encounter_id=scope["encounter_id"],
                patient_id=scope["patient_id"],
                issuer_id=scope["issuer_id"],
                expected_version=2,
            )
        assert not PrescriptionDocument.objects.exists()


def test_database_enforces_binding_rls_and_immutability(
    rbac_graph: RbacGraph,
) -> None:
    scope, document = seed_rendered(rbac_graph)
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        # Forged bindings are rejected by the database guard, not the service.
        for field, value in (
            ("patient_id", uuid4()),
            ("issuer_id", uuid4()),
            ("document_version", 9),
            ("state", "issued"),
            ("input_digest", "z" * 64),
            ("qr_handle", "has spaces"),
            ("pdf_bytes", b"plain text"),
        ):
            fields = {
                "organization_id": rbac_graph.organization_a,
                "draft_id": document.draft_id,
                "encounter_id": scope["encounter_id"],
                "clinic_id": scope["clinic_id"],
                "patient_id": scope["patient_id"],
                "issuer_id": scope["issuer_id"],
                "document_version": 2,
                "state": "rendered",
                "render_params": {"renderer": "synthetic-pdf-v1"},
                "frozen_input": {"v": "clinic-prescription-document-v1"},
                "input_digest": "a" * 64,
                "pdf_digest": "b" * 64,
                "pdf_bytes": b"%PDF-1.4 body",
                "qr_handle": uuid4().hex + "a" * 11,
            }
            fields[field] = value
            with pytest.raises(DatabaseError), transaction.atomic():
                PrescriptionDocument.objects.create(**fields)
        assert PrescriptionDocument.objects.count() == 1
    with setup_context(rbac_graph.organization_a), connection.cursor() as cursor:
        cursor.execute(
            "SELECT relrowsecurity,relforcerowsecurity FROM pg_class "
            "WHERE relname='prescription_prescriptiondocument'"
        )
        assert cursor.fetchall() == [(True, True)]
        cursor.execute(
            "SELECT policyname FROM pg_policies "
            "WHERE schemaname='clinic_app' "
            "AND tablename='prescription_prescriptiondocument'"
        )
        assert {row[0] for row in cursor.fetchall()} == {
            "setup_tenant",
            "document_read",
            "document_insert",
        }
        cursor.execute(
            "SELECT privilege_type FROM information_schema.role_table_grants "
            "WHERE grantee='clinic_app' AND table_schema='clinic_app' "
            "AND table_name='prescription_prescriptiondocument'"
        )
        assert {row[0] for row in cursor.fetchall()} == {"SELECT", "INSERT"}


def test_peer_physician_without_care_is_denied(rbac_graph: RbacGraph) -> None:
    scope, document = seed_rendered(rbac_graph)
    peer = User.objects.create(username=f"synthetic-peer-{uuid4().hex}")
    with setup_context(rbac_graph.organization_a):
        UserClinicRole.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_a,
            user=peer,
            role="physician",
        )
    with runtime_role(), tenant_context(peer.pk, rbac_graph.organization_a):
        assert not PrescriptionDocument.objects.exists()
        with pytest.raises(ClinicalAccessDeniedError):
            download_document(clinic_id=scope["clinic_id"], document_id=document.pk)


def test_membership_revocation_closes_access(rbac_graph: RbacGraph) -> None:
    scope, document = seed_rendered(rbac_graph)
    with setup_context(rbac_graph.organization_a):
        UserClinicRole.objects.filter(
            user_id=rbac_graph.physician, role="physician"
        ).delete()
    with (
        pytest.raises(TenantAccessDeniedError),
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        download_document(clinic_id=scope["clinic_id"], document_id=document.pk)


class _FormPayload(HTMLParser):
    """Collect the exact hidden inputs and named buttons of one form."""

    def __init__(self) -> None:
        super().__init__()
        self.fields: dict[str, str] = {}
        self.buttons: dict[str, set[str]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        name = values.get("name") or ""
        if not name:
            return
        if tag == "input" and values.get("type") == "hidden":
            self.fields[name] = values.get("value") or ""
        elif tag == "button":
            self.buttons.setdefault(name, set()).add(values.get("value") or "")


def _submit_form(
    client: Client, html: str, url: str, action: str
) -> _MonkeyPatchedWSGIResponse:
    """POST one rendered form verbatim: its hidden inputs plus its button."""
    parser = _FormPayload()
    for markup in re.findall(r"<form\b.*?</form>", html, flags=re.DOTALL):
        parser.feed(markup)
        if action in parser.buttons.get("action", ()):
            return client.post(url, {**parser.fields, "action": action})
        parser.fields.clear()
        parser.buttons.clear()
    message = f"no rendered form carries action={action}"
    raise AssertionError(message)


def test_http_document_forms_submit_as_rendered(rbac_graph: RbacGraph) -> None:
    """The shipped render/download buttons must reach the authorized path."""
    scope = seed(rbac_graph)
    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        draft = create_draft(**scope, category=SYNTHETIC_CATEGORY)
        save_draft(
            **scope,
            draft_id=draft.pk,
            category=SYNTHETIC_CATEGORY,
            expected_version=1,
            items=[ITEM],
        )
    url = f"/prescription/clinics/{rbac_graph.clinic_a}/draft/"
    with physician_client(rbac_graph) as client:
        assert (
            client.post(
                url, {"action": "open", "encounter_id": scope["encounter_id"]}
            ).status_code
            == 302
        )
        page = client.get(url)
        assert page.status_code == 200
        # Submit exactly what the rendered form carries — no extra fields.
        rendered = _submit_form(client, page.content.decode(), url, "render_document")
        assert rendered.status_code == 302
        page = client.get(url)
        assert page.status_code == 200
        download = _submit_form(client, page.content.decode(), url, "download_document")
        assert download.status_code == 200
        assert download.headers["Content-Type"] == "application/pdf"
        assert "no-store" in download.headers["Cache-Control"]
    with setup_context(rbac_graph.organization_a):
        document = PrescriptionDocument.objects.get()
        assert bytes(document.pdf_bytes) == download.content


@pytest.mark.parametrize("case", ["vertical-tab-header", "joined-reference-token"])
def test_http_lexical_failure_preserves_original_and_allows_recovery(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    scope, original = seed_rendered(rbac_graph)

    class MalformedRenderer:
        renderer_id = "malformed-lexical-profile"

        def render(self, frozen_input: Mapping[str, object]) -> RenderedDocument:
            valid = SyntheticPdfRenderer().render(frozen_input).pdf_bytes
            # Retain the real frozen fields, QR and three-page content; change
            # only the lexical defect and rebuild all xref offsets.
            objects = re.findall(rb"\d+ 0 obj\n(.*?)\nendobj\n", valid, re.DOTALL)
            assert b"/Kids [3 0 R 5 0 R 7 0 R]" in objects[1]
            if case == "vertical-tab-header":
                malformed = _xref_pdf(objects, header_separator=b"\x0b")
            else:
                objects[1] = objects[1].replace(b"R 5", b"R5", 1)
                malformed = _xref_pdf(objects)
            with pytest.raises(DocumentRenderError):
                validate_pdf_structure(malformed)
            return RenderedDocument(
                pdf_bytes=malformed,
                pdf_digest=sha256(malformed).hexdigest(),
            )

    with (
        runtime_role(),
        tenant_context(rbac_graph.physician, rbac_graph.organization_a),
    ):
        save_draft(
            **scope,
            draft_id=original.draft_id,
            category=SYNTHETIC_CATEGORY,
            expected_version=2,
            items=[ITEM] * 12,
        )
    url = f"/prescription/clinics/{scope['clinic_id']}/draft/"
    render_fields = {
        "action": "render_document",
        "encounter_id": scope["encounter_id"],
        "expected_version": "3",
    }
    download_fields = {
        "action": "download_document",
        "encounter_id": scope["encounter_id"],
        "document_id": original.pk,
    }
    with physician_client(rbac_graph) as client:
        assert (
            client.post(
                url,
                {
                    "action": "open",
                    "encounter_id": scope["encounter_id"],
                },
            ).status_code
            == 302
        )
        with monkeypatch.context() as patch:
            patch.setattr(
                "apps.prescription.services.default_renderer", MalformedRenderer
            )
            assert client.post(url, render_fields).status_code == 503
        retained = client.post(url, download_fields)
        assert retained.status_code == 200
        assert retained.content == bytes(original.pdf_bytes)
        with tenant_context(rbac_graph.physician, rbac_graph.organization_a):
            stored = PrescriptionDocument.objects.get()
            assert stored.pk == original.pk
            for field in (
                "pdf_digest",
                "input_digest",
                "qr_handle",
                "document_version",
                "frozen_input",
                "render_params",
            ):
                assert getattr(stored, field) == getattr(original, field)
            assert bytes(stored.pdf_bytes) == retained.content
        assert client.post(url, render_fields).status_code == 302
        with tenant_context(rbac_graph.physician, rbac_graph.organization_a):
            recovered = PrescriptionDocument.objects.get(document_version=3)
        delivered = client.post(url, {**download_fields, "document_id": recovered.pk})
        assert delivered.status_code == 200
        assert delivered.headers["Content-Type"] == "application/pdf"
        assert "no-store" in delivered.headers["Cache-Control"]
        assert delivered.content == bytes(recovered.pdf_bytes)
        assert sha256(delivered.content).hexdigest() == recovered.pdf_digest
    result = subprocess.run(  # noqa: S603 - fixed poppler tool and delivered bytes
        [_poppler("pdftotext"), "-", "-"],
        input=delivered.content,
        capture_output=True,
        check=True,
        timeout=15,
    )
    assert not result.stderr
    pages = result.stdout.decode().split("\f")
    assert len(pages) == 4
    assert not pages[-1]
    assert all(page.strip() for page in pages[:-1])
    text = "".join(pages)
    for field in ("issuer_label", "patient_label", "clinic_label", "issued_at"):
        assert recovered.frozen_input[field] in text
    for number in range(1, 13):
        assert re.search(rf"\bItem {number}\b", text)


def _poppler(tool: str) -> str:
    path = shutil.which(tool)
    if path is None:
        # CI exports CLINIC_PDF_TOOLS=required so a missing poppler binary
        # fails the gate instead of silently skipping artifact inspection.
        if os.environ.get("CLINIC_PDF_TOOLS", "") == "required":
            pytest.fail(
                f"required PDF inspection tool {tool} (poppler) is not installed"
            )
        pytest.skip(f"{tool} (poppler) is not installed")
    return path


def test_long_labels_stay_on_page_and_clear_of_qr() -> None:
    """Render the longest supported labels and inspect the real page output.

    Poppler extracts every word's bounding box and rasterizes the page; the
    QR modules are sampled from the raster and compared with the expected
    symbol, so overlap or clipping cannot hide behind vector assertions.
    """
    frozen_input: dict[str, object] = {
        "v": "clinic-prescription-document-v1",
        "document_version": 2,
        "contract_version": "synthetic-draft-v1",
        "issuer_label": "W" * 60,
        "patient_label": "W" * 60,
        "clinic_label": "Clínica Sintética",
        "issued_at": "2026-09-21T10:00:00+00:00",
        "verification_url": "https://verify.clinic-os.invalid/v/" + "A" * 43,
        "input_digest": "a" * 64,
        "items": [
            {
                "medication_description": "Medicamento sintético",
                "strength_form": "Forma sintética",
                "dose": "Dose sintética",
                "route": "Via sintética",
                "frequency": "Frequência sintética",
                "duration": "Duração sintética",
                "quantity": "Quantidade sintética",
                "instructions": "",
            }
        ],
    }
    pdf = SyntheticPdfRenderer().render(frozen_input).pdf_bytes
    with TemporaryDirectory() as tmp:
        pdf_path = Path(tmp) / "document.pdf"
        pdf_path.write_bytes(pdf)
        bbox = subprocess.run(  # noqa: S603 - fixed poppler tool, test-owned file
            [_poppler("pdftotext"), "-bbox", str(pdf_path), "-"],
            capture_output=True,
            check=True,
        ).stdout.decode()
        subprocess.run(  # noqa: S603 - fixed poppler tool, test-owned file
            [
                _poppler("pdftoppm"),
                "-f",
                "1",
                "-singlefile",
                "-scale-to",
                "1100",
                "-png",
                str(pdf_path),
                str(Path(tmp) / "page"),
            ],
            capture_output=True,
            check=True,
        )
        image = Image.open(Path(tmp) / "page.png").convert("L")
    pages = re.split(r"<page ", bbox)[1:]
    assert pages
    page_words: list[tuple[float, float, list[tuple[float, float, float, float]]]] = []
    for chunk in pages:
        width_match = re.search(r'width="([\d.]+)"', chunk)
        height_match = re.search(r'height="([\d.]+)"', chunk)
        assert width_match is not None
        assert height_match is not None
        width = float(width_match.group(1))
        height = float(height_match.group(1))
        words = [
            (
                float(match.group(1)),
                float(match.group(2)),
                float(match.group(3)),
                float(match.group(4)),
            )
            for match in re.finditer(
                r'<word xMin="([\d.]+)" yMin="([\d.]+)" '
                r'xMax="([\d.]+)" yMax="([\d.]+)">',
                chunk,
            )
        ]
        for x_min, y_min, x_max, y_max in words:
            assert 0 <= x_min < x_max <= width
            assert 0 <= y_min < y_max <= height
        page_words.append((width, height, words))
    page_width, page_height, first_page_words = page_words[0]
    assert first_page_words
    # The long labels survive wrapping: their fragments rejoin into the value.
    word_text = "".join(
        match.group(1) for match in re.finditer(r"<word [^>]*>([^<]*)</word>", bbox)
    )
    assert "W" * 60 in word_text
    # The drawn QR bounds come from the PDF itself: dark rects cover the code
    # area; the quiet zone extends QR_BORDER modules beyond it on each side.
    cells = _qr_cells(pdf)
    code_left = min(x for x, _ in cells)
    code_right = max(x for x, _ in cells) + QR_MODULE_PT
    code_bottom = min(y for _, y in cells)
    code_top = max(y for _, y in cells) + QR_MODULE_PT
    border_pt = QR_BORDER * QR_MODULE_PT
    qr_left = code_left - border_pt
    qr_right = code_right + border_pt
    qr_bottom = code_bottom - border_pt
    qr_top = code_top + border_pt
    for x_min, y_min, x_max, y_max in first_page_words:
        # pdftotext y is top-origin; the QR bounds are bottom-origin points.
        overlaps = (
            x_min < qr_right
            and x_max > qr_left
            and page_height - y_max < qr_top
            and page_height - y_min > qr_bottom
        )
        assert not overlaps
    # Sample the rasterized page: every expected QR module must render dark
    # and every quiet-zone module must render light. Matrix indices include
    # the border, so they are offset by QR_BORDER from the code area.
    code = qrcode.QRCode(border=QR_BORDER, box_size=1)
    code.add_data(frozen_input["verification_url"])
    code.make(fit=True)
    matrix = code.get_matrix()
    scale_x = image.width / page_width
    scale_y = image.height / page_height
    for row, row_cells in enumerate(matrix):
        for column, dark in enumerate(row_cells):
            px = int((code_left + (column - QR_BORDER + 0.5) * QR_MODULE_PT) * scale_x)
            py = int(
                (page_height - code_top + (row - QR_BORDER + 0.5) * QR_MODULE_PT)
                * scale_y
            )
            pixel = image.getpixel((px, py))
            assert isinstance(pixel, int)
            assert (pixel < 128) == bool(dark)
