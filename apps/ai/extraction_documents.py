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
from typing import IO, cast

from PIL import Image, ImageOps, UnidentifiedImageError

from apps.ai.extraction_contracts import ExtractionInputError, Region

MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
MAX_PAGES = 32
MAX_PIXELS = 25_000_000
MAX_INPUT_BYTES = 1_048_576
MAX_RENDER_BYTES = 12 * 1024 * 1024
PROCESS_TIMEOUT = 10
PIPE_CHUNK = 65536


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
        return _poppler(
            [
                "/usr/bin/pdftoppm",
                "-f",
                str(page),
                "-l",
                str(page),
                "-singlefile",
                "-scale-to",
                "1400",
                "-png",
                "-",
            ],
            data,
        )
    image = _image(data)
    image.thumbnail((1400, 1400))
    output = BytesIO()
    image.convert("RGB").save(output, format="PNG")
    return output.getvalue()


class _Lines(HTMLParser):
    """Read Poppler's inert XHTML word boxes without an XML entity resolver."""

    def __init__(self, rotations: tuple[int, ...]) -> None:
        super().__init__(convert_charrefs=True)
        self.rotations = rotations
        self.page = 0
        self.width = 0.0
        self.height = 0.0
        self.box: tuple[int, int, int, int] | None = None
        self.words: list[str] = []
        self.in_word = False
        self.lines: list[tuple[int, tuple[int, int, int, int], str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "page":
            self.page += 1
            self.width = float(values.get("width") or "0")
            self.height = float(values.get("height") or "0")
            if self.rotations[self.page - 1] in (90, 270):
                self.width, self.height = self.height, self.width
        elif tag == "line":
            if self.width <= 0 or self.height <= 0:
                raise ExtractionInputError
            self.words = []
            self.box = (
                round(float(values.get("xmin") or "0") / self.width * 10000),
                round(float(values.get("ymin") or "0") / self.height * 10000),
                round(float(values.get("xmax") or "0") / self.width * 10000),
                round(float(values.get("ymax") or "0") / self.height * 10000),
            )
        elif tag == "word":
            self.in_word = True

    def handle_data(self, data: str) -> None:
        if self.in_word:
            self.words.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "word":
            self.in_word = False
        elif tag == "line" and self.box:
            self.lines.append((self.page, self.box, " ".join(self.words)))
            self.box = None


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
        reader = _Lines(rotations)
        reader.feed(
            _poppler(["/usr/bin/pdftotext", "-bbox-layout", "-", "-"], data).decode(
                "utf-8", errors="replace"
            )
        )
        lines = (
            (
                page,
                Region(page, box).display_bbox((360 - rotations[page - 1]) % 360),
                raw,
            )
            for page, box, raw in reader.lines
        )
        for page, bbox, raw in sorted(
            lines, key=lambda line: (line[0], line[1][1], line[1][0])
        ):
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
                    "page": page,
                    "bbox": bbox,
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
