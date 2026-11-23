"""Bounded local document geometry and an offline synthetic extraction adapter."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import selectors
import subprocess
import time
from html.parser import HTMLParser
from io import BytesIO
from math import isfinite
from pathlib import Path
from typing import IO, TYPE_CHECKING, cast

from PIL import Image, ImageOps, UnidentifiedImageError

from apps.ai.extraction_contracts import (
    COORDINATE_SCALE,
    MAX_CANDIDATES,
    ExtractionInputError,
    PdfValueEvidence,
    Region,
    value_transcription,
)

if TYPE_CHECKING:
    from apps.ai.extraction_contracts import SourceEvidence

PDF_CONTENT_TYPE = "application/pdf"
MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
MAX_PAGES = 32
MAX_PIXELS = 25_000_000
MAX_INPUT_BYTES = 1_048_576
MAX_RENDER_BYTES = 12 * 1024 * 1024
REVIEW_LONG_EDGE = 1400
PROCESS_TIMEOUT = 10
PIPE_CHUNK = 65536
_FIELD = re.compile(r"[a-z][a-z0-9_]{0,31}")
type _Box = tuple[float, float, float, float]

# Anchor transcription of the only reviewed raster, independent of the
# recorded provider reply. Any other raster requires manual review.
_SCAN_SHA256 = "311f5a521d517dc8ff1175499959d5e9726f3dc8c0c193268f019539c93be2da"
_SCAN_REGIONS = {
    Region(1, (500, 3583, 2488, 4042)): "glucose: 123,40 mg/dL [70,0 - 99,0]",
    Region(1, (500, 5875, 2562, 6333)): "hemoglobin: 12,50 g/dL [12,0 - 16,0]",
}


def _poppler(command: list[str], data: bytes) -> bytes:
    output = bytearray()
    try:
        process = subprocess.Popen(  # noqa: S603 - fixed executables and flags
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
        )
        stdin = cast("IO[bytes]", process.stdin)
        stdout = cast("IO[bytes]", process.stdout)
        deadline = time.monotonic() + PROCESS_TIMEOUT
        with process, selectors.DefaultSelector() as ready:
            os.set_blocking(stdin.fileno(), False)
            os.set_blocking(stdout.fileno(), False)
            ready.register(stdin, selectors.EVENT_WRITE)
            ready.register(stdout, selectors.EVENT_READ)
            offset = 0
            try:
                while ready.get_map():
                    remaining = deadline - time.monotonic()
                    events = ready.select(max(0, remaining))
                    if remaining <= 0 or not events:
                        raise ExtractionInputError
                    for key, event in events:
                        if event == selectors.EVENT_WRITE:
                            offset += os.write(
                                key.fd, data[offset : offset + PIPE_CHUNK]
                            )
                            if offset == len(data):
                                ready.unregister(stdin)
                                stdin.close()
                        else:
                            chunk = os.read(key.fd, PIPE_CHUNK)
                            if not chunk:
                                ready.unregister(stdout)
                                continue
                            output.extend(chunk)
                            if len(output) > MAX_RENDER_BYTES:
                                raise ExtractionInputError
                if process.wait(timeout=max(0, deadline - time.monotonic())):
                    raise ExtractionInputError
            finally:
                process.kill()
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ExtractionInputError from error
    return bytes(output)


def _image(data: bytes) -> Image.Image:
    try:
        image = Image.open(BytesIO(data))
        if image.width * image.height > MAX_PIXELS or image.format not in (
            "JPEG",
            "PNG",
        ):
            raise ExtractionInputError
        return ImageOps.exif_transpose(image)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as error:
        raise ExtractionInputError from error


def document_pages(*, data: bytes, content_type: str) -> tuple[int, ...]:
    """Read source geometry, not caller-selected page counts or rotations."""
    if not data or len(data) > MAX_DOCUMENT_BYTES:
        raise ExtractionInputError
    if content_type in ("image/jpeg", "image/png"):
        _image(data)
        return (0,)
    if content_type != PDF_CONTENT_TYPE or not data.startswith(b"%PDF-"):
        raise ExtractionInputError
    info = _poppler(
        ["/usr/bin/pdfinfo", "-f", "1", "-l", str(MAX_PAGES), "-box", "-"], data
    ).decode("utf-8", errors="replace")
    count = re.search(r"(?m)^Pages:\s+([0-9]+)$", info)
    if count is None or not 1 <= int(count[1]) <= MAX_PAGES:
        raise ExtractionInputError
    rotations = {
        int(page): int(rotation) % 360
        for page, rotation in re.findall(
            r"(?m)^Page\s+([0-9]+)\s+rot:\s+(-?[0-9]+)$", info
        )
    }
    if len(rotations) != int(count[1]):
        raise ExtractionInputError
    result = tuple(rotations[p] for p in range(1, int(count[1]) + 1))
    if any(rotation not in (0, 90, 180, 270) for rotation in result):
        raise ExtractionInputError
    return result


def document_input(*, data: bytes, content_type: str) -> str:
    """Bind exact bytes and geometry in the existing encrypted gateway artifact."""
    rotations = document_pages(data=data, content_type=content_type)
    payload = json.dumps(
        {
            "schema": "document-extraction-v1",
            "content_type": content_type,
            "sha256": hashlib.sha256(data).hexdigest(),
            "rotations": rotations,
            "data": base64.b64encode(data).decode("ascii"),
        },
        separators=(",", ":"),
    )
    if len(payload.encode()) > MAX_INPUT_BYTES:
        raise ExtractionInputError
    return payload


def render_page(*, data: bytes, content_type: str, page: int) -> bytes:
    """Render inert PNG bytes; original PDF scripts never enter the browser."""
    rotations = document_pages(data=data, content_type=content_type)
    if type(page) is not int or not 1 <= page <= len(rotations):
        raise ExtractionInputError
    if content_type == PDF_CONTENT_TYPE:
        return _render_pdf_page(data, page=page)
    image = _image(data)
    image.thumbnail((REVIEW_LONG_EDGE, REVIEW_LONG_EDGE))
    output = BytesIO()
    image.convert("RGB").save(output, format="PNG")
    return output.getvalue()


def _render_pdf_page(data: bytes, *, page: int) -> bytes:
    return _poppler(
        [
            "/usr/bin/pdftoppm",
            "-f",
            str(page),
            "-l",
            str(page),
            "-singlefile",
            "-scale-to",
            str(REVIEW_LONG_EDGE),
            "-png",
            "-",
        ],
        data,
    )


class _Lines(HTMLParser):
    """Read bounded text layout for PDF value drafts."""

    def __init__(self, rotations: tuple[int, ...]) -> None:
        super().__init__(convert_charrefs=True)
        self.rotations = rotations
        self.sizes: list[tuple[float, float]] = []
        self.box: _Box | None = None
        self.words: list[str] = []
        self.in_word = False
        self.word: list[str] = []
        self.lines: list[tuple[Region, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "page":
            width = float(values.get("width") or "0")
            height = float(values.get("height") or "0")
            self.sizes.append(
                (height, width)
                if self.rotations[len(self.sizes)] in (90, 270)
                else (width, height)
            )
        elif tag == "line":
            self.words = []
            self.box = _text_box(values)
        elif tag == "word":
            self.in_word = True
            self.word = []

    def handle_data(self, data: str) -> None:
        if self.in_word:
            self.word.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "word" and self.in_word:
            self.words.append("".join(self.word))
            self.in_word = False
        elif tag == "line" and self.box is not None:
            number = len(self.sizes)
            width, height = self.sizes[-1]
            left, top, right, bottom = self.box
            columns = _normalize_axis(left, right, width)
            rows = _normalize_axis(top, bottom, height)
            if columns is not None and rows is not None:
                rotated = Region(number, (columns[0], rows[0], columns[1], rows[1]))
                bbox = rotated.display_bbox(-self.rotations[number - 1] % 360)
                self.lines.append((Region(number, bbox), " ".join(self.words)))
            self.box = None


def _normalize_axis(start: float, end: float, size: float) -> tuple[int, int] | None:
    """Clip draft geometry before rounding; keep any positive visible extent."""
    if not all(map(isfinite, (start, end, size))) or size <= 0:
        raise ExtractionInputError
    first, last = max(0.0, start), min(size, end)
    if first >= last:
        return None
    left = min(COORDINATE_SCALE - 1, round(first / size * COORDINATE_SCALE))
    right = max(left + 1, round(last / size * COORDINATE_SCALE))
    return left, right


def _text_box(attrs: dict[str, str | None]) -> _Box:
    return (
        float(attrs.get("xmin") or "0"),
        float(attrs.get("ymin") or "0"),
        float(attrs.get("xmax") or "0"),
        float(attrs.get("ymax") or "0"),
    )


def _draft(region: Region, text: str) -> dict[str, object] | None:
    label, colon, remainder = text.partition(":")
    field = label.replace(" ", "")
    if not colon or _FIELD.fullmatch(field) is None:
        return None
    tokens, bracket, interval = remainder.strip().partition(" [")
    words = tokens.split()
    if not words:
        return None
    value, *tail = words
    # Only discard a single, nonnumeric suffix. Never recognize a unit, join
    # number fragments, or manufacture an independent match for a prefix.
    if tail and (
        len(tail) != 1
        or re.fullmatch(r"[^\W\d_]+(?:/[^\W\d_]+[0-9]?)?|%", tail[0]) is None
    ):
        # Do not copy the ambiguous remainder (possibly a proposed PDF unit)
        # into any value field, even when the result will be refused.
        value = ""
    interval = interval.removesuffix("]") if bracket else ""
    return {
        "field": field,
        "kind": "date" if field == "date" else "decimal",
        "raw_text": value_transcription(field, value, interval),
        "value": value,
        "unit": "",
        "reference_interval": interval,
        "page": region.page,
        "bbox": region.bbox,
        "legible": True,
    }


def _unreadable(page: int) -> dict[str, object]:
    return {
        "field": "unreadable",
        "kind": "text",
        "raw_text": "",
        "value": "",
        "unit": "",
        "reference_interval": "",
        "page": page,
        "bbox": [0, 0, 10000, 10000],
        "legible": False,
    }


def _pdf_layout(data: bytes, rotations: tuple[int, ...]) -> _Lines:
    reader = _Lines(rotations)
    reader.feed(
        _poppler(
            ["/usr/bin/pdftotext", "-l", str(len(rotations)), "-bbox-layout", "-", "-"],
            data,
        ).decode("utf-8", errors="replace")
    )
    return reader


def source_regions(
    *, data: bytes, content_type: str, rotations: tuple[int, ...]
) -> SourceEvidence:
    """Load anchored PDF value drafts or the exact hash-bound image transcription."""
    if document_pages(data=data, content_type=content_type) != rotations:
        raise ExtractionInputError
    if content_type == PDF_CONTENT_TYPE:
        lines: dict[Region, str] = {}
        seen = set()
        for region, text in _pdf_layout(data, rotations).lines:
            draft = _draft(region, text)
            if region in seen:
                lines.pop(region, None)
            elif draft is not None:
                lines[region] = cast("str", draft["raw_text"])
            seen.add(region)
        return PdfValueEvidence(lines)
    if content_type == "image/png" and hashlib.sha256(data).hexdigest() == _SCAN_SHA256:
        return _SCAN_REGIONS.copy()
    return {}


def synthetic_document_extraction(content: str) -> str:
    """Draft explicit synthetic PDF values and intervals, never units or tools.

    This is not OCR or general medical understanding. Raster-only pages receive
    an unreadable region. Real OCR/model providers remain BLOCKED-ON-EG.
    """
    try:
        payload = json.loads(content)
        if (
            not isinstance(payload, dict)
            or set(payload) != {"schema", "content_type", "sha256", "rotations", "data"}
            or payload["schema"] != "document-extraction-v1"
        ):
            raise ExtractionInputError
        data = base64.b64decode(payload["data"], validate=True)
        rotations = document_pages(data=data, content_type=payload["content_type"])
        if (
            hashlib.sha256(data).hexdigest() != payload["sha256"]
            or list(rotations) != payload["rotations"]
        ):
            raise ExtractionInputError
    except (ValueError, TypeError, KeyError, binascii.Error) as error:
        raise ExtractionInputError from error
    rows: list[dict[str, object]] = []
    if payload["content_type"] == PDF_CONTENT_TYPE:
        for region, text in sorted(
            _pdf_layout(data, rotations).lines,
            key=lambda line: (line[0].page, line[0].bbox[1], line[0].bbox[0]),
        ):
            draft = _draft(region, text)
            if draft is not None:
                rows.append(draft)
                if len(rows) > MAX_CANDIDATES:
                    raise ExtractionInputError
    for page in range(1, len(rotations) + 1):
        if not any(row["page"] == page for row in rows):
            rows.append(_unreadable(page))
    return json.dumps({"candidates": rows}, separators=(",", ":"))


def recorded_document_extraction(content: str) -> str:
    """Replay one reviewed synthetic scan, bound to the exact image digest."""
    synthetic_document_extraction(content)
    payload = json.loads(content)
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "document_extraction_v1.json").read_text(
            encoding="utf-8"
        )
    )
    if (
        fixture["schema_version"] != 1
        or fixture["data_class"] != "synthetic"
        or fixture["model_id"] != "extraction-fixture-v1"
        or fixture["sha256"] != payload["sha256"]
        or fixture["content_type"] != payload["content_type"]
    ):
        raise ExtractionInputError
    return json.dumps({"candidates": fixture["candidates"]}, separators=(",", ":"))
