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
from pathlib import Path
from typing import IO, TYPE_CHECKING, cast

from PIL import Image, ImageChops, ImageOps, UnidentifiedImageError

from apps.ai.extraction_contracts import (
    MAX_CANDIDATES,
    ExtractionInputError,
    Region,
    parse_unit,
)
from apps.ai.extraction_raster import (
    Box,
    GlyphOrigins,
    Page,
    Reference,
    TextLine,
    matching_ink,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
MAX_PAGES = 32
MAX_PIXELS = 25_000_000
MAX_INPUT_BYTES = 1_048_576
MAX_RENDER_BYTES = 12 * 1024 * 1024
REVIEW_LONG_EDGE = 1400
PROCESS_TIMEOUT = 10
PIPE_CHUNK = 65536
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_CHUNK_HEADER = 8
PNG_CHUNK_OVERHEAD = 12
_UNIT_LINE = re.compile(
    r"(?P<prefix>[a-z][a-z0-9_]{0,31}: *[^ \[\]]+) "
    r"(?P<unit>[^ \[\]]+(?: [^ \[\]]+)*)(?P<interval> \[.*\])?"
)

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
    if content_type != "application/pdf" or not data.startswith(b"%PDF-"):
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
    if content_type == "application/pdf":
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
    """Read Poppler's inert XHTML word boxes without an XML entity resolver."""

    def __init__(self, rotations: tuple[int, ...]) -> None:
        super().__init__(convert_charrefs=True)
        self.rotations = rotations
        self.pages: list[Page] = []
        self.box: Box | None = None
        self.first_word: Box | None = None
        self.words: list[str] = []
        self.in_word = False
        self.word: list[str] = []
        self.lines: list[TextLine] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "page":
            self.pages.append(
                Page(
                    float(values.get("width") or "0"),
                    float(values.get("height") or "0"),
                    self.rotations[len(self.pages)],
                )
            )
        elif tag == "line":
            self.words = []
            self.box = _text_box(values)
            self.first_word = None
        elif tag == "word":
            if self.first_word is None:
                self.first_word = _text_box(values)
            self.in_word = True
            self.word = []

    def handle_data(self, data: str) -> None:
        if self.in_word:
            self.word.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "word" and self.in_word:
            self.words.append("".join(self.word))
            self.in_word = False
        elif tag == "line" and self.box and self.first_word:
            page = self.pages[-1]
            width, height = page.display_size
            if not page.valid:
                raise ExtractionInputError
            left, top, right, bottom = self.box
            rotated = Region(
                len(self.pages),
                (
                    round(left / width * 10000),
                    round(top / height * 10000),
                    round(right / width * 10000),
                    round(bottom / height * 10000),
                ),
            )
            region = Region(rotated.page, rotated.display_bbox(-page.rotation % 360))
            self.lines.append(
                TextLine(
                    region,
                    page.unrotate(self.box),
                    page.unrotate(self.first_word),
                    _unit_line(" ".join(self.words)),
                )
            )
            self.box = None


def _text_box(attrs: dict[str, str | None]) -> Box:
    return (
        float(attrs.get("xmin") or "0"),
        float(attrs.get("ymin") or "0"),
        float(attrs.get("xmax") or "0"),
        float(attrs.get("ymax") or "0"),
    )


def _unit_line(text: str) -> str:
    match = _UNIT_LINE.fullmatch(text)
    if match is None:
        return text
    unit = parse_unit(match["unit"].replace(" ", ""))
    if unit is None:
        return text
    # This is only a hypothesis until the positioned visible ink passes.
    return f"{match['prefix']} {unit}{match['interval'] or ''}"


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


def _png_frames(data: bytes, *, count: int) -> Iterator[bytes]:
    offset = 0
    for _ in range(count):
        start = offset
        if not data.startswith(PNG_SIGNATURE, offset):
            raise ExtractionInputError
        offset += len(PNG_SIGNATURE)
        while offset + PNG_CHUNK_OVERHEAD <= len(data):
            size = int.from_bytes(data[offset : offset + 4], "big")
            kind = data[offset + 4 : offset + PNG_CHUNK_HEADER]
            offset += size + PNG_CHUNK_OVERHEAD
            if offset > len(data):
                raise ExtractionInputError
            if kind == b"IEND" and not size:
                yield data[start:offset]
                break
        else:
            raise ExtractionInputError
    if offset != len(data):
        raise ExtractionInputError


def _reference_frames(
    reference: Reference, *, size: tuple[int, int]
) -> Iterator[Image.Image]:
    # Without an output root Poppler streams one PNG per page to stdout.
    data = _poppler(
        [
            "/usr/bin/pdftoppm",
            "-f",
            "1",
            "-l",
            str(reference.pages),
            "-scale-to",
            str(REVIEW_LONG_EDGE),
            "-png",
            "-",
        ],
        reference.pdf,
    )
    for index, frame in enumerate(_png_frames(data, count=reference.pages)):
        with Image.open(BytesIO(frame)) as image:
            if image.size != size or image.mode != "RGB":
                raise ExtractionInputError
            yield ImageChops.invert(image.convert("L")) if index else image.copy()


def _visible_pdf_regions(data: bytes, rotations: tuple[int, ...]) -> dict[Region, str]:
    layout = _pdf_layout(data, rotations)
    regions: dict[Region, str] = {}
    for number, page in enumerate(layout.pages, start=1):
        lines = [line for line in layout.lines if line.region.page == number]
        if not page.valid or not 0 < len(lines) <= MAX_CANDIDATES:
            continue
        glyphs = GlyphOrigins()
        try:
            positions = _poppler(
                [
                    "/usr/bin/pdftocairo",
                    "-svg",
                    "-f",
                    str(number),
                    "-l",
                    str(number),
                    "-",
                    "-",
                ],
                data,
            )
        except ExtractionInputError:
            # A page with unavailable or oversized position evidence is unresolved.
            continue
        glyphs.feed(positions.decode("utf-8", errors="replace"))
        reference = page.reference(lines=lines, points=glyphs.points)
        if reference is None:
            continue
        with Image.open(BytesIO(_render_pdf_page(data, page=number))) as source:
            try:
                if source.mode == "RGB":
                    proven = matching_ink(
                        source=source,
                        frames=_reference_frames(reference, size=source.size),
                        claims=reference.claims,
                        rotation=page.rotation,
                    )
                    regions.update(proven)
            except (ExtractionInputError, OSError):
                continue
    return regions


def source_regions(
    *, data: bytes, content_type: str, rotations: tuple[int, ...]
) -> dict[Region, str]:
    """Verify rendered PDF lines or use the exact hash-bound reviewed image."""
    if document_pages(data=data, content_type=content_type) != rotations:
        raise ExtractionInputError
    if content_type != "application/pdf":
        if (
            content_type == "image/png"
            and hashlib.sha256(data).hexdigest() == _SCAN_SHA256
        ):
            return _SCAN_REGIONS.copy()
        return {}
    return _visible_pdf_regions(data, rotations)


def synthetic_document_extraction(content: str) -> str:
    """Parse explicit synthetic `field: value unit [interval]` lines, never tools.

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
    if payload["content_type"] == "application/pdf":
        for line in sorted(
            _pdf_layout(data, rotations).lines,
            key=lambda line: (
                line.region.page,
                line.region.bbox[1],
                line.region.bbox[0],
            ),
        ):
            region, raw = line.region, line.text
            match = re.fullmatch(r"([a-z][a-z0-9_]{0,31}):\s*(.+)", raw)
            if match is None:
                continue
            value_unit, separator, interval = match[2].partition(" [")
            value, _, unit = value_unit.partition(" ")
            rows.append(
                {
                    "field": match[1],
                    "kind": "date" if match[1] == "date" else "decimal",
                    "raw_text": raw,
                    "value": value,
                    "unit": unit,
                    "reference_interval": interval.removesuffix("]")
                    if separator
                    else "",
                    "page": region.page,
                    "bbox": region.bbox,
                    "legible": True,
                }
            )
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
