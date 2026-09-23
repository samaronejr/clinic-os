"""Deterministic synthetic PDF rendering boundary for prescription artifacts.

The task-6 capability register records ``pdf_rendering`` as unavailable: no
renderer package is approved and none may be installed. This module therefore
provides an explicitly synthetic renderer behind a protocol so an approved
capability can replace it without changing the artifact contract. The
synthetic renderer is deterministic — identical frozen input produces
byte-identical output — and it refuses to run outside synthetic data mode,
so rendering/dependency unavailability blocks real output explicitly.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from typing import Protocol

from django.conf import settings

RENDERER_ID = "synthetic-pdf-v1"
RENDER_PROFILE = "clinic-synthetic-document-v1"
DOCUMENT_INPUT_VERSION = "clinic-prescription-document-v1"
PAGE_WIDTH = 595
PAGE_HEIGHT = 842
MARGIN_X = 56
TOP_Y = 800
BOTTOM_Y = 56
LEADING = 14
FONT_SIZE = 11
FONT_NAME = "Helvetica"
QR_MODULE_PT = 3
QR_BORDER = 4
QR_MAX_HEIGHT_PT = 160
QR_TEXT_GAP = 8
MAX_PDF_BYTES = 2 * 1024 * 1024
PDF_MAGIC = b"%PDF-"
_CONTROL_FLOOR = 0x20
_DELETE_CODEPOINT = 0x7F
_MAX_GLYPH_WIDTH = 1000
# Helvetica AFM widths in 1/1000 em for the printable WinAnsi (cp1252) range.
# The profile uses a proportional font, so wrapping must measure glyph width;
# a character count cannot keep text inside the page or clear of the QR block.
_HELVETICA_WIDTHS = {
    0x20: 278,
    0x21: 278,
    0x22: 355,
    0x23: 556,
    0x24: 556,
    0x25: 889,
    0x26: 667,
    0x27: 191,
    0x28: 333,
    0x29: 333,
    0x2A: 389,
    0x2B: 584,
    0x2C: 278,
    0x2D: 333,
    0x2E: 278,
    0x2F: 278,
    0x30: 556,
    0x31: 556,
    0x32: 556,
    0x33: 556,
    0x34: 556,
    0x35: 556,
    0x36: 556,
    0x37: 556,
    0x38: 556,
    0x39: 556,
    0x3A: 278,
    0x3B: 278,
    0x3C: 584,
    0x3D: 584,
    0x3E: 584,
    0x3F: 556,
    0x40: 1015,
    0x41: 667,
    0x42: 667,
    0x43: 722,
    0x44: 722,
    0x45: 667,
    0x46: 611,
    0x47: 778,
    0x48: 722,
    0x49: 278,
    0x4A: 500,
    0x4B: 667,
    0x4C: 556,
    0x4D: 833,
    0x4E: 722,
    0x4F: 778,
    0x50: 667,
    0x51: 778,
    0x52: 722,
    0x53: 667,
    0x54: 611,
    0x55: 722,
    0x56: 667,
    0x57: 944,
    0x58: 667,
    0x59: 667,
    0x5A: 611,
    0x5B: 278,
    0x5C: 278,
    0x5D: 278,
    0x5E: 469,
    0x5F: 556,
    0x60: 333,
    0x61: 556,
    0x62: 556,
    0x63: 500,
    0x64: 556,
    0x65: 556,
    0x66: 278,
    0x67: 556,
    0x68: 556,
    0x69: 222,
    0x6A: 222,
    0x6B: 500,
    0x6C: 222,
    0x6D: 833,
    0x6E: 556,
    0x6F: 556,
    0x70: 556,
    0x71: 556,
    0x72: 333,
    0x73: 500,
    0x74: 278,
    0x75: 556,
    0x76: 500,
    0x77: 722,
    0x78: 500,
    0x79: 500,
    0x7A: 500,
    0x7B: 334,
    0x7C: 260,
    0x7D: 334,
    0x7E: 584,
    0x80: 556,
    0x82: 222,
    0x83: 556,
    0x84: 333,
    0x85: 1000,
    0x86: 556,
    0x87: 556,
    0x88: 333,
    0x89: 1000,
    0x8A: 667,
    0x8B: 333,
    0x8C: 1000,
    0x8E: 611,
    0x91: 222,
    0x92: 222,
    0x93: 333,
    0x94: 333,
    0x95: 350,
    0x96: 556,
    0x97: 1000,
    0x98: 333,
    0x99: 1000,
    0x9A: 500,
    0x9B: 333,
    0x9C: 944,
    0x9E: 500,
    0x9F: 667,
    0xA0: 278,
    0xA1: 333,
    0xA2: 556,
    0xA3: 556,
    0xA4: 556,
    0xA5: 556,
    0xA6: 260,
    0xA7: 556,
    0xA8: 333,
    0xA9: 737,
    0xAA: 370,
    0xAB: 556,
    0xAC: 584,
    0xAD: 333,
    0xAE: 737,
    0xAF: 333,
    0xB0: 400,
    0xB1: 584,
    0xB2: 333,
    0xB3: 333,
    0xB4: 333,
    0xB5: 556,
    0xB6: 537,
    0xB7: 278,
    0xB8: 333,
    0xB9: 333,
    0xBA: 365,
    0xBB: 556,
    0xBC: 834,
    0xBD: 834,
    0xBE: 834,
    0xBF: 611,
    0xC0: 667,
    0xC1: 667,
    0xC2: 667,
    0xC3: 667,
    0xC4: 667,
    0xC5: 667,
    0xC6: 1000,
    0xC7: 722,
    0xC8: 667,
    0xC9: 667,
    0xCA: 667,
    0xCB: 667,
    0xCC: 278,
    0xCD: 278,
    0xCE: 278,
    0xCF: 278,
    0xD0: 722,
    0xD1: 722,
    0xD2: 778,
    0xD3: 778,
    0xD4: 778,
    0xD5: 778,
    0xD6: 778,
    0xD7: 584,
    0xD8: 778,
    0xD9: 722,
    0xDA: 722,
    0xDB: 722,
    0xDC: 722,
    0xDD: 667,
    0xDE: 667,
    0xDF: 611,
    0xE0: 556,
    0xE1: 556,
    0xE2: 556,
    0xE3: 556,
    0xE4: 556,
    0xE5: 556,
    0xE6: 889,
    0xE7: 500,
    0xE8: 556,
    0xE9: 556,
    0xEA: 556,
    0xEB: 556,
    0xEC: 278,
    0xED: 278,
    0xEE: 278,
    0xEF: 278,
    0xF0: 556,
    0xF1: 556,
    0xF2: 556,
    0xF3: 556,
    0xF4: 556,
    0xF5: 556,
    0xF6: 556,
    0xF7: 584,
    0xF8: 611,
    0xF9: 556,
    0xFA: 556,
    0xFB: 556,
    0xFC: 556,
    0xFD: 500,
    0xFE: 556,
    0xFF: 500,
}
_PDF_WHITESPACE = b"\x00\x09\x0a\x0c\x0d\x20"
_PDF_DELIMITERS = b"()<>[]{}/%"
_PDF_SPACE = b"[" + _PDF_WHITESPACE + b"]"
_XREF_SUBSECTION = re.compile(rb"(\d+) (\d+)" + _PDF_SPACE + rb"*\n")
_XREF_ENTRY = re.compile(rb"(\d{10}) (\d{5}) ([nf])(?: \r| \n|\r\n)")
_STARTXREF = re.compile(
    rb"startxref"
    + _PDF_SPACE
    + rb"+(\d+)"
    + _PDF_SPACE
    + rb"+%%EOF"
    + _PDF_SPACE
    + rb"*\Z"
)
_OBJECT_HEADER = re.compile(
    rb"(\d+)"
    + _PDF_SPACE
    + rb"+(\d+)"
    + _PDF_SPACE
    + rb"+obj(?=["
    + re.escape(_PDF_WHITESPACE + _PDF_DELIMITERS)
    + rb"]|\Z)"
)
_PDF_NUMBER = re.compile(rb"[+-]?\d*\.?\d+\Z")
_PDF_KEYWORDS = {b"true": True, b"false": False, b"null": None}


class _PdfName(bytes):
    """A PDF name object, kept distinct from opaque string content."""


@dataclass(frozen=True)
class _IndirectRef:
    """An indirect reference ``number generation R`` in the object graph."""

    number: int
    generation: int


_PdfValue = (
    None
    | bool
    | int
    | float
    | bytes
    | _PdfName
    | _IndirectRef
    | list["_PdfValue"]
    | dict[bytes, "_PdfValue"]
)


class DocumentRenderingError(Exception):
    """Report a rendering-boundary failure without echoing caller content."""


class RenderingUnavailableError(DocumentRenderingError):
    """Report that no approved rendering capability is usable here."""


class DocumentRenderError(DocumentRenderingError):
    """Report input or output that fails the fixed artifact contract."""


@dataclass(frozen=True, kw_only=True)
class RenderedDocument:
    """Fully materialized render output; bytes are immutable once returned."""

    pdf_bytes: bytes
    pdf_digest: str


class DocumentRenderer(Protocol):
    """Render one frozen document input into final immutable bytes."""

    renderer_id: str

    def render(self, frozen_input: Mapping[str, object]) -> RenderedDocument:
        """Produce deterministic PDF bytes or raise a rendering error."""
        ...


def _require_text(frozen_input: Mapping[str, object], key: str) -> str:
    value = frozen_input.get(key)
    if not isinstance(value, str) or not value.strip():
        raise DocumentRenderError
    return value


def _display_text(value: str) -> str:
    """Normalize line breaks for one-line PDF text; reject other controls."""
    flattened = " ".join(value.split())
    if any(
        ord(character) < _CONTROL_FLOOR or ord(character) == _DELETE_CODEPOINT
        for character in flattened
    ):
        raise DocumentRenderError
    try:
        flattened.encode("cp1252")
    except UnicodeEncodeError as error:
        # The synthetic single-font profile cannot draw this glyph; the
        # failure is explicit, never a silent substitution.
        raise DocumentRenderError from error
    return flattened


def _wrap(label: str, value: str) -> str:
    """Join a label and its normalized value into one wrappable paragraph."""
    return f"{label}: {_display_text(value)}"


def _qr_matrix(payload: str) -> list[list[bool]]:
    """Encode the public verification payload with the installed QR support."""
    try:
        import qrcode  # noqa: PLC0415 - declared dependency boundary
        from qrcode.constants import ERROR_CORRECT_M  # noqa: PLC0415
    except ImportError as error:
        raise RenderingUnavailableError from error
    code = qrcode.QRCode(border=QR_BORDER, box_size=1, error_correction=ERROR_CORRECT_M)
    code.add_data(payload)
    code.make(fit=True)
    matrix = code.get_matrix()
    if not matrix or any(len(row) != len(matrix) for row in matrix):
        raise DocumentRenderError
    if len(matrix) * QR_MODULE_PT > QR_MAX_HEIGHT_PT:
        raise DocumentRenderError
    return [[bool(cell) for cell in row] for row in matrix]


def _paragraphs(frozen_input: Mapping[str, object]) -> list[str]:
    """Lay out the required visible fields in a fixed, deterministic order.

    Each entry is one unwrapped paragraph; ``_layout_lines`` wraps it to the
    width available at its final vertical position.
    """
    items = frozen_input.get("items")
    if not isinstance(items, Sequence) or isinstance(items, str | bytes) or not items:
        raise DocumentRenderError
    document_version = frozen_input.get("document_version")
    contract_version = _require_text(frozen_input, "contract_version")
    if type(document_version) is not int or document_version < 1:
        raise DocumentRenderError
    paragraphs = [
        "Prescrição — documento sintético",
        f"Versão do documento: {document_version} · Contrato: {contract_version}",
        _wrap("Emitente", _require_text(frozen_input, "issuer_label")),
        _wrap("Paciente", _require_text(frozen_input, "patient_label")),
        _wrap("Clínica", _require_text(frozen_input, "clinic_label")),
        _wrap("Emitido em", _require_text(frozen_input, "issued_at")),
        "",
        "Itens",
    ]
    for position, item in enumerate(items, start=1):
        if not isinstance(item, Mapping):
            raise DocumentRenderError
        paragraphs.append(f"Item {position}")
        for field in (
            "medication_description",
            "strength_form",
            "dose",
            "route",
            "frequency",
            "duration",
            "quantity",
        ):
            value = item.get(field)
            if not isinstance(value, str) or not value.strip():
                raise DocumentRenderError
            paragraphs.append(_wrap(f"  {field}", value))
        instructions = item.get("instructions")
        if not isinstance(instructions, str):
            raise DocumentRenderError
        if instructions.strip():
            paragraphs.append(_wrap("  instructions", instructions))
    paragraphs.extend(
        [
            "",
            "Documento sintético de ensaio. Não é uma receita válida.",
            _wrap("Verificação", _require_text(frozen_input, "verification_url")),
            _wrap("Resumo do conteúdo", _require_text(frozen_input, "input_digest")),
        ]
    )
    return paragraphs


def _text_width(text: str) -> float:
    """Measure the rendered width of one line in points at FONT_SIZE."""
    units = sum(
        _HELVETICA_WIDTHS.get(ord(character), _MAX_GLYPH_WIDTH) for character in text
    )
    return units * FONT_SIZE / 1000


def _fit_prefix(word: str, width: float) -> int:
    """Return the longest prefix of ``word`` that fits inside ``width`` points."""
    total = 0.0
    for index, character in enumerate(word):
        total += (
            _HELVETICA_WIDTHS.get(ord(character), _MAX_GLYPH_WIDTH) * FONT_SIZE / 1000
        )
        if total > width:
            return max(index, 1)
    return len(word)


def _wrap_to_width(paragraph: str, line_width: Callable[[int], float]) -> list[str]:
    """Greedily wrap one paragraph; ``line_width`` gives each line's budget."""
    if not paragraph:
        return [""]
    lines: list[str] = []
    line = ""
    for word in paragraph.split(" "):
        candidate = f"{line} {word}" if line else word
        if _text_width(candidate) <= line_width(len(lines)):
            line = candidate
            continue
        fragment = word
        if line:
            keep = _fit_prefix(candidate, line_width(len(lines)))
            if keep > len(line) + 1:
                # Fill this line with the fitting fragment of the long word.
                lines.append(candidate[:keep])
                fragment = word[keep - len(line) - 1 :]
            else:
                lines.append(line)
            line = ""
        # A word wider than the line splits at the exact fitting boundary.
        while _text_width(fragment) > line_width(len(lines)):
            cut = _fit_prefix(fragment, line_width(len(lines)))
            lines.append(fragment[:cut])
            fragment = fragment[cut:]
        line = fragment
    if line:
        lines.append(line)
    return lines


def _layout_lines(paragraphs: list[str], qr_size: int) -> list[str]:
    """Wrap paragraphs so no line leaves the page or touches the QR block.

    The QR occupies the top right of the first page; lines whose baseline
    falls inside that band get a narrower budget that keeps a fixed gap
    between the text and the QR's quiet zone.
    """
    qr_left = PAGE_WIDTH - MARGIN_X - qr_size * QR_MODULE_PT
    qr_top = TOP_Y + FONT_SIZE
    qr_bottom = qr_top - qr_size * QR_MODULE_PT

    def line_width(index: int) -> float:
        baseline = TOP_Y - index * LEADING
        if qr_bottom - LEADING < baseline < qr_top + LEADING:
            return qr_left - QR_TEXT_GAP - MARGIN_X
        return PAGE_WIDTH - 2 * MARGIN_X

    lines: list[str] = []
    for paragraph in paragraphs:
        lines.extend(_wrap_to_width(paragraph, line_width))
    return lines


def _paginate(lines: list[str]) -> list[list[str]]:
    per_page = (TOP_Y - BOTTOM_Y) // LEADING
    return [lines[index : index + per_page] for index in range(0, len(lines), per_page)]


def _escape_text(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _content_stream(
    lines: list[str],
    *,
    font_resource: str,
    qr_matrix: list[list[bool]] | None,
) -> bytes:
    parts = [f"BT /{font_resource} {FONT_SIZE} Tf {MARGIN_X} {TOP_Y} Td {LEADING} TL"]
    parts.extend(f"T* ({_escape_text(line)}) Tj" for line in lines)
    parts.append("ET")
    if qr_matrix is not None:
        parts.append("0 0 0 rg")
        size = len(qr_matrix)
        for row_index, row in enumerate(qr_matrix):
            for column, dark in enumerate(row):
                if dark:
                    x = (
                        PAGE_WIDTH
                        - MARGIN_X
                        - size * QR_MODULE_PT
                        + column * QR_MODULE_PT
                    )
                    y = TOP_Y + FONT_SIZE - (row_index + 1) * QR_MODULE_PT
                    parts.append(f"{x} {y} {QR_MODULE_PT} {QR_MODULE_PT} re f")
    # Text operands are WinAnsi (cp1252); _display_text guarantees encodability.
    return ("\n".join(parts) + "\n").encode("cp1252")


def _build_pdf(pages: list[bytes]) -> bytes:
    """Assemble a minimal xref-table PDF with fixed object numbering."""
    page_count = len(pages)
    font_id = 3 + 2 * page_count
    kids = " ".join(f"{3 + 2 * index} 0 R" for index in range(page_count))
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {page_count} >>".encode("ascii"),
    ]
    for index, stream in enumerate(pages):
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_WIDTH} "
                f"{PAGE_HEIGHT}] /Resources << /Font << /F1 {font_id} 0 R >> >> "
                f"/Contents {4 + 2 * index} 0 R >>"
            ).encode("ascii")
        )
        objects.append(
            b"<< /Length "
            + str(len(stream)).encode("ascii")
            + b" >>\nstream\n"
            + stream
            + b"endstream"
        )
    objects.append(
        f"<< /Type /Font /Subtype /Type1 /BaseFont /{FONT_NAME} "
        "/Encoding /WinAnsiEncoding >>".encode("ascii")
    )
    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, body in enumerate(objects, start=1):
        offsets.append(len(output))
        output += f"{number} 0 obj\n".encode("ascii") + body + b"\nendobj\n"
    xref_at = len(output)
    output += f"xref\n0 {len(objects) + 1}\n".encode("ascii")
    output += b"0000000000 65535 f \n"
    output += b"".join(
        f"{offset:010d} 00000 n \n".encode("ascii") for offset in offsets[1:]
    )
    output += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_at}\n%%EOF\n"
    ).encode("ascii")
    return bytes(output)


def _skip_whitespace(data: bytes, position: int) -> int:
    """Skip PDF whitespace (not vertical tab) and ``%`` end-of-line comments."""
    while position < len(data):
        if data[position] in _PDF_WHITESPACE:
            position += 1
            continue
        if data.startswith(b"%", position):
            end = position + 1
            while end < len(data) and data[end] not in b"\r\n":
                end += 1
            position = end
            continue
        break
    return position


def _parse_literal_string(data: bytes, position: int) -> tuple[bytes, int]:
    """Consume a ``(...)`` literal, honoring escapes and nested parentheses.

    The content is opaque: bytes inside a literal string are never PDF
    syntax, so a ``/Pages 2 0 R`` substring there is text, not a reference.
    """
    depth = 1
    index = position + 1
    while index < len(data):
        byte = data[index : index + 1]
        if byte == b"\\":
            index += 2
            continue
        if byte == b"(":
            depth += 1
        elif byte == b")":
            depth -= 1
            if depth == 0:
                return data[position : index + 1], index + 1
        index += 1
    raise DocumentRenderError


def _parse_hex_string(data: bytes, position: int) -> tuple[bytes, int]:
    """Consume a ``<...>`` hex string; its content is opaque like a literal."""
    end = data.find(b">", position + 1)
    if end < 0:
        raise DocumentRenderError
    content = data[position + 1 : end]
    if any(
        byte not in _PDF_WHITESPACE and byte not in b"0123456789abcdefABCDEF"
        for byte in content
    ):
        raise DocumentRenderError
    return data[position : end + 1], end + 1


def _parse_token(data: bytes, position: int) -> tuple[bytes, int]:
    """Consume one regular token up to whitespace or a delimiter."""
    end = position
    while (
        end < len(data)
        and data[end] not in _PDF_WHITESPACE
        and data[end] not in _PDF_DELIMITERS
    ):
        end += 1
    if end == position:
        raise DocumentRenderError
    return data[position:end], end


def _parse_name(data: bytes, position: int) -> tuple[_PdfName, int]:
    """Consume a ``/name`` token, decoding ``#xx`` hex escapes.

    PDF name semantics make ``#`` plus two hex digits a single byte, so
    ``/Pa#67es`` is the name ``/Pages``: keys must compare equal here or
    the validator reads a different dictionary than a real reader. A
    ``#`` that is not followed by two hex digits is malformed and fails
    closed. The decoded bytes must still be name characters: an escape
    producing NUL, whitespace or a delimiter — ``/Pages#00`` decodes to
    ``Pages`` plus a NUL byte — is not a name readers agree on and fails
    closed.
    """
    token, end = _parse_token(data, position + 1)
    if b"#" not in token:
        return _PdfName(token), end
    decoded = bytearray()
    index = 0
    while index < len(token):
        if token[index] == ord("#"):
            if index + 2 >= len(token):
                raise DocumentRenderError
            try:
                decoded.append(int(token[index + 1 : index + 3], 16))
            except ValueError as error:
                raise DocumentRenderError from error
            index += 3
            continue
        decoded.append(token[index])
        index += 1
    if any(byte in _PDF_WHITESPACE or byte in _PDF_DELIMITERS for byte in decoded):
        raise DocumentRenderError
    return _PdfName(bytes(decoded)), end


def _number(token: bytes) -> int | float:
    """Convert a numeric token; anything else is not a number."""
    if _PDF_NUMBER.match(token) is None:
        raise DocumentRenderError
    if b"." in token:
        return float(token)
    return int(token)


def _parse_number_or_ref(
    data: bytes, position: int
) -> tuple[int | float | _IndirectRef, int]:
    """Consume a number, or ``N G R`` when two integers precede ``R``."""
    token, end = _parse_token(data, position)
    if token.isdigit():
        lookahead = _skip_whitespace(data, end)
        if lookahead < len(data) and data[lookahead] not in _PDF_DELIMITERS:
            second, after = _parse_token(data, lookahead)
            if second.isdigit():
                marker = _skip_whitespace(data, after)
                if data.startswith(b"R", marker):
                    reference_marker, reference_end = _parse_token(data, marker)
                    if reference_marker != b"R":
                        raise DocumentRenderError
                    return _IndirectRef(int(token), int(second)), reference_end
    return _number(token), end


def _parse_array(data: bytes, position: int) -> tuple[list[_PdfValue], int]:
    """Consume a ``[...]`` array of parsed values."""
    items: list[_PdfValue] = []
    position += 1
    while True:
        position = _skip_whitespace(data, position)
        if position >= len(data):
            raise DocumentRenderError
        if data.startswith(b"]", position):
            return items, position + 1
        item, position = _parse_value(data, position)
        items.append(item)


def _parse_dict(data: bytes, position: int) -> tuple[dict[bytes, _PdfValue], int]:
    """Consume a ``<<...>>`` dictionary of name keys and parsed values."""
    entries: dict[bytes, _PdfValue] = {}
    position += 2
    while True:
        position = _skip_whitespace(data, position)
        if data[position : position + 2] == b">>":
            return entries, position + 2
        if position >= len(data) or not data.startswith(b"/", position):
            raise DocumentRenderError
        key, position = _parse_name(data, position)
        position = _skip_whitespace(data, position)
        value, position = _parse_value(data, position)
        if key in entries:
            # A repeated key is ambiguous: readers disagree on whether the
            # first or last entry wins, so the document graph is not the
            # same for every reader. Fail closed instead of guessing.
            raise DocumentRenderError
        entries[key] = value


def _parse_value(data: bytes, position: int) -> tuple[_PdfValue, int]:
    """Consume one PDF value; unsupported or malformed syntax fails closed."""
    result: tuple[_PdfValue, int]
    byte = data[position : position + 1]
    if byte == b"(":
        result = _parse_literal_string(data, position)
    elif byte == b"<":
        if data[position : position + 2] == b"<<":
            result = _parse_dict(data, position)
        else:
            result = _parse_hex_string(data, position)
    elif byte == b"[":
        result = _parse_array(data, position)
    elif byte == b"/":
        result = _parse_name(data, position)
    elif byte.isdigit() or byte in (b"+", b"-"):
        result = _parse_number_or_ref(data, position)
    else:
        token, end = _parse_token(data, position)
        if token not in _PDF_KEYWORDS:
            raise DocumentRenderError
        result = _PDF_KEYWORDS[token], end
    return result


@dataclass(frozen=True)
class _PdfStream:
    """An unfiltered stream whose direct Length and delimiters were checked."""

    content: bytes


_PdfObject = _PdfValue | _PdfStream


def _parse_stream(
    data: bytes, position: int, value: _PdfValue
) -> tuple[_PdfStream, int]:
    """Accept only the renderer's direct Length dictionary, without filters."""
    if not isinstance(value, dict) or set(value) != {b"Length"}:
        raise DocumentRenderError
    length = value[b"Length"]
    if type(length) is not int or length < 0:
        raise DocumentRenderError
    start = position + len(b"stream\n")
    end = start + length
    if not data.startswith(b"stream\n", position) or not data.startswith(
        b"endstream\n", end
    ):
        raise DocumentRenderError
    return _PdfStream(data[start:end]), end + len(b"endstream\n")


def _parse_indirect_object(data: bytes, position: int) -> _PdfObject:
    """Consume exactly one xref-bounded object, including its stream if any."""
    value, position = _parse_value(data, _skip_whitespace(data, position))
    position = _skip_whitespace(data, position)
    result: _PdfObject = value
    if data.startswith(b"stream", position):
        result, position = _parse_stream(data, position, value)
    if not data.startswith(b"endobj", position):
        raise DocumentRenderError
    tail = position + len(b"endobj")
    if tail < len(data) and data[tail] not in _PDF_WHITESPACE:
        raise DocumentRenderError
    if _skip_whitespace(data, tail) != len(data):
        raise DocumentRenderError
    return result


def _parse_objects(
    pdf: bytes, entries: Mapping[int, tuple[int, int]], xref_at: int
) -> dict[_IndirectRef, _PdfObject]:
    """Parse every in-use object, forbidding overlapping or unindexed bodies."""
    objects: dict[_IndirectRef, _PdfObject] = {}
    ordered = sorted((offset, number, gen) for number, (offset, gen) in entries.items())
    position = _skip_whitespace(pdf, len(b"%PDF-1.4\n"))
    for index, (offset, number, generation) in enumerate(ordered):
        end = ordered[index + 1][0] if index + 1 < len(ordered) else xref_at
        if offset != position or not offset < end <= xref_at:
            raise DocumentRenderError
        data = pdf[offset:end]
        if not data.endswith(b"\n"):
            # A trailing comment must not hide the next xref object's header.
            raise DocumentRenderError
        header = _OBJECT_HEADER.match(data)
        if (
            header is None
            or int(header.group(1)) != number
            or int(header.group(2)) != generation
        ):
            raise DocumentRenderError
        objects[_IndirectRef(number, generation)] = _parse_indirect_object(
            data, header.end()
        )
        position = end
    return objects


def _resolve_object(
    objects: Mapping[_IndirectRef, _PdfObject], reference: _PdfValue
) -> _PdfObject:
    """Resolve an exact object number and generation, never a textual look-alike."""
    if not isinstance(reference, _IndirectRef) or reference not in objects:
        raise DocumentRenderError
    return objects[reference]


def _xref_offset(pdf: bytes) -> int:
    """Return the ``startxref`` target or reject the byte envelope."""
    if (
        not pdf.startswith(b"%PDF-1.4\n")
        or not pdf.rstrip(_PDF_WHITESPACE).endswith(b"%%EOF")
        or len(pdf) > MAX_PDF_BYTES
    ):
        raise DocumentRenderError
    startxref = _STARTXREF.search(pdf)
    if startxref is None:
        raise DocumentRenderError
    return int(startxref.group(1))


def _xref_table(
    pdf: bytes, position: int
) -> tuple[int, dict[int, tuple[int, int]], int]:
    """Consume every xref subsection; return count, entries and position.

    In-use (``n``) entries map each object number to its byte offset and
    generation, so indirect references resolve through the table instead
    of a raw byte search that string or stream content could spoof.
    """
    objects = 0
    entries: dict[int, tuple[int, int]] = {}
    while not pdf[position:].lstrip(_PDF_WHITESPACE).startswith(b"trailer"):
        subsection = _XREF_SUBSECTION.match(pdf, position)
        if subsection is None or int(subsection.group(1)) != objects:
            raise DocumentRenderError
        position = subsection.end()
        for _ in range(int(subsection.group(2))):
            entry = _XREF_ENTRY.match(pdf, position)
            if entry is None:
                raise DocumentRenderError
            if objects == 0:
                if entry.group(0) != b"0000000000 65535 f \n":
                    raise DocumentRenderError
            elif entry.group(3) == b"n" and entry.group(2) < b"65535":
                entries[objects] = (int(entry.group(1)), int(entry.group(2)))
            else:
                raise DocumentRenderError
            objects += 1
            position = entry.end()
    return objects, entries, position


def _parse_trailer(data: bytes) -> dict[bytes, _PdfValue]:
    """Parse the ``trailer`` keyword and its dictionary to the slice end."""
    position = _skip_whitespace(data, 0)
    if not data.startswith(b"trailer", position):
        raise DocumentRenderError
    position = _skip_whitespace(data, position + len(b"trailer"))
    value, position = _parse_value(data, position)
    if (
        _skip_whitespace(data, position) != len(data)
        or not isinstance(value, dict)
        or set(value) != {b"Size", b"Root"}
    ):
        raise DocumentRenderError
    return value


def _require_dictionary(value: _PdfObject, keys: set[bytes]) -> dict[bytes, _PdfValue]:
    """Require the complete key set, rejecting extensions as well as omissions."""
    if not isinstance(value, dict) or set(value) != keys:
        raise DocumentRenderError
    return value


def _require_content_stream(value: _PdfObject) -> None:
    r"""Whitelist the writer's text and QR operators, not arbitrary PDF programs.

    Text is opaque, with only the writer's escapes for ``()``, and ``\``.
    No filters, external resources, inline images or additional operators
    are part of this synthetic profile.
    """
    if not isinstance(value, _PdfStream):
        raise DocumentRenderError
    header = (f"BT /F1 {FONT_SIZE} Tf {MARGIN_X} {TOP_Y} Td {LEADING} TL\n").encode(
        "ascii"
    )
    pattern = (
        re.escape(header)
        + rb"(?:T\* \((?:[^()\\\r\n]|\\[()\\])*\) Tj\n)+ET\n"
        + rb"(?:0 0 0 rg\n(?:\d{1,3} \d{1,3} "
        + f"{QR_MODULE_PT} {QR_MODULE_PT} re f\n".encode("ascii")
        + rb")+)?"
    )
    if re.fullmatch(pattern, value.content) is None:
        raise DocumentRenderError


def _require_page(
    objects: Mapping[_IndirectRef, _PdfObject],
    page: dict[bytes, _PdfValue],
    parent: _IndirectRef,
) -> set[_IndirectRef]:
    """Check the A4 page, its inline resources, font and content stream."""
    box = page[b"MediaBox"]
    if (
        page[b"Type"] != _PdfName(b"Page")
        or page[b"Parent"] != parent
        or not isinstance(box, list)
        or any(type(item) is not int for item in box)
        or box != [0, 0, PAGE_WIDTH, PAGE_HEIGHT]
    ):
        raise DocumentRenderError
    resources = _require_dictionary(page[b"Resources"], {b"Font"})
    fonts = _require_dictionary(resources[b"Font"], {b"F1"})
    font_ref = fonts[b"F1"]
    content_ref = page[b"Contents"]
    if not isinstance(font_ref, _IndirectRef) or not isinstance(
        content_ref, _IndirectRef
    ):
        raise DocumentRenderError
    font = _resolve_object(objects, font_ref)
    if font != {
        b"Type": _PdfName(b"Font"),
        b"Subtype": _PdfName(b"Type1"),
        b"BaseFont": _PdfName(FONT_NAME.encode("ascii")),
        b"Encoding": _PdfName(b"WinAnsiEncoding"),
    }:
        raise DocumentRenderError
    _require_content_stream(_resolve_object(objects, content_ref))
    return {font_ref, content_ref}


def _require_page_tree(
    objects: Mapping[_IndirectRef, _PdfObject], root: _PdfValue
) -> None:
    """Require a flat, fully reachable graph of only the writer's object types."""
    catalog = _require_dictionary(_resolve_object(objects, root), {b"Type", b"Pages"})
    pages_ref = catalog[b"Pages"]
    if (
        catalog[b"Type"] != _PdfName(b"Catalog")
        or not isinstance(root, _IndirectRef)
        or not isinstance(pages_ref, _IndirectRef)
    ):
        raise DocumentRenderError
    pages = _require_dictionary(
        _resolve_object(objects, pages_ref), {b"Type", b"Kids", b"Count"}
    )
    kids = pages[b"Kids"]
    if pages[b"Type"] != _PdfName(b"Pages") or not isinstance(kids, list) or not kids:
        raise DocumentRenderError
    count = pages[b"Count"]
    if type(count) is not int or count != len(kids):
        raise DocumentRenderError
    used = {root, pages_ref}
    page_refs: set[_IndirectRef] = set()
    for kid in kids:
        if not isinstance(kid, _IndirectRef) or kid in page_refs:
            raise DocumentRenderError
        page_refs.add(kid)
        page = _require_dictionary(
            _resolve_object(objects, kid),
            {b"Type", b"Parent", b"MediaBox", b"Contents", b"Resources"},
        )
        used.update(_require_page(objects, page, pages_ref))
    if used | page_refs != set(objects):
        # Unreferenced dictionaries/streams are not a way to smuggle in types
        # outside Catalog, Pages, Page, the fixed Font and page content.
        raise DocumentRenderError


def validate_pdf_structure(pdf: bytes) -> None:
    """Accept only the unencrypted synthetic renderer profile, not general PDF.

    Every xref object is bounded, parsed and reachable through the whitelisted
    catalog/page/resource graph. Every dictionary has an exact key set and
    checked values, including the trailer (Size/Root only), fixed Helvetica
    font and unfiltered streams with exact direct Length. Unsupported PDF
    features fail closed even when valid in another PDF profile. Decoded
    names and exact reference generations retain their PDF semantics.
    """
    try:
        xref_at = _xref_offset(pdf)
        keyword = re.match(rb"xref" + _PDF_SPACE + rb"+", pdf[xref_at:])
        if keyword is None:
            raise DocumentRenderError
        count, entries, position = _xref_table(pdf, xref_at + keyword.end())
        trailer_end = pdf.find(b"startxref", position)
        if trailer_end < 0:
            raise DocumentRenderError
        trailer = _parse_trailer(pdf[position:trailer_end])
        size = trailer[b"Size"]
        if type(size) is not int or size != count:
            raise DocumentRenderError
        objects = _parse_objects(pdf, entries, xref_at)
        _require_page_tree(objects, trailer[b"Root"])
    except (RecursionError, ValueError) as error:
        # Recursion and oversized numeric tokens are malformed boundary data.
        raise DocumentRenderError from error


class SyntheticPdfRenderer:
    """Deterministic single-font PDF writer for the synthetic rehearsal only.

    Rendering behavior is fixed by ``RENDER_PROFILE``: A4 page box, Helvetica
    at 11pt with 14pt leading, glyph-width wrapping that reserves the
    first-page QR block, and no timestamps or unstable identifiers. The
    same frozen input always produces the same bytes. It is not an approved
    clinical renderer and refuses to run outside synthetic data mode.
    """

    renderer_id = RENDERER_ID

    def render(self, frozen_input: Mapping[str, object]) -> RenderedDocument:
        """Render the frozen input or fail explicitly; output is validated."""
        if settings.CLINIC_DATA_MODE != "synthetic":
            raise RenderingUnavailableError
        if frozen_input.get("v") != DOCUMENT_INPUT_VERSION:
            raise DocumentRenderError
        matrix = _qr_matrix(_require_text(frozen_input, "verification_url"))
        lines = _layout_lines(_paragraphs(frozen_input), len(matrix))
        pages = _paginate(lines)
        streams = [
            _content_stream(
                page_lines,
                font_resource="F1",
                qr_matrix=matrix if index == 0 else None,
            )
            for index, page_lines in enumerate(pages)
        ]
        pdf_bytes = _build_pdf(streams)
        validate_pdf_structure(pdf_bytes)
        return RenderedDocument(
            pdf_bytes=pdf_bytes, pdf_digest=sha256(pdf_bytes).hexdigest()
        )


def default_renderer() -> DocumentRenderer:
    """Resolve the configured renderer; only the synthetic one exists."""
    return SyntheticPdfRenderer()
