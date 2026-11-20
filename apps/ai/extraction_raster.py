"""Verify the paint of the bounded synthetic PDF text format, not its text layer."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from html.parser import HTMLParser
from itertools import pairwise
from math import ceil, floor, isfinite
from typing import TYPE_CHECKING

from PIL import Image as RasterImage
from PIL import ImageChops

from apps.ai.extraction_contracts import MAX_RAW_TEXT, source_unit_span

if TYPE_CHECKING:
    from collections.abc import Iterable

    from PIL.Image import Image

    from apps.ai.extraction_contracts import Region

type Box = tuple[float, float, float, float]
type Crop = tuple[int, int, int, int]
type Point = tuple[float, float]
type Paint = tuple[int, str]
MIN_STROKE_HEIGHT = 8
GAP_HEIGHT_DIVISOR = 3  # At most one third of the tallest unit glyph.
INK_MARGIN = 3
HELVETICA_HEIGHT = 0.925
# Treat low-coverage antialias fringes as background for ownership only.
# The equality check below still compares every original RGB sample.
INK_CORE = (0,) * 128 + (255,) * 128


class GlyphOrigins(HTMLParser):
    """Read only nonempty glyph origins from local Cairo SVG; do not render SVG."""

    def __init__(self) -> None:
        """Keep coordinate hints and IDs, never source SVG paths or text."""
        super().__init__(convert_charrefs=True)
        self.points: list[Point] = []
        self._in_defs = False
        self._glyph = ""
        self._nonempty: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Index outlined definitions and collect positions of their uses."""
        values = dict(attrs)
        if tag == "defs":
            self._in_defs = True
        elif self._in_defs:
            if tag in ("g", "symbol"):
                self._glyph = values.get("id") or ""
            elif tag == "path" and values.get("d") and self._glyph.startswith("glyph"):
                self._nonempty.add(f"#{self._glyph}")
        elif (
            tag == "use"
            and (values.get("xlink:href") or values.get("href")) in self._nonempty
        ):
            x, y = values.get("x"), values.get("y")
            if x is not None and y is not None:
                self.points.append((float(x), float(y)))

    def handle_endtag(self, tag: str) -> None:
        """Close definition scopes without interpreting the graphics tree."""
        if tag == "defs":
            self._in_defs = False
        elif self._in_defs and tag in ("g", "symbol"):
            self._glyph = ""


@dataclass(frozen=True, slots=True, repr=False)
class TextLine:
    """One text-layer hypothesis and its unrotated bounds in points."""

    region: Region
    box: Box
    text: str


@dataclass(frozen=True, slots=True, repr=False)
class Reference:
    """A canonical page followed by separate nonunit and unit-glyph ink layers."""

    pdf: bytes
    pages: int


@dataclass(frozen=True, slots=True, repr=False)
class ReferenceInk:
    """Canonical RGB paint and isolated gray ink layers."""

    page: Image
    layers: tuple[Image, ...]


@dataclass(frozen=True, slots=True)
class Page:
    """The page geometry shared by projection and the review renderer."""

    width: float
    height: float
    rotation: int

    @property
    def valid(self) -> bool:
        """Refuse invalid renderer geometry before matching or projecting boxes."""
        return all(isfinite(edge) and edge > 0 for edge in (self.width, self.height))

    @property
    def display_size(self) -> tuple[float, float]:
        """Return the rotated width and height in points."""
        return (
            (self.height, self.width)
            if self.rotation in (90, 270)
            else (self.width, self.height)
        )

    def unrotate(self, box: Box) -> Box:
        """Undo the page rotation without normalizing away sub-pixel positions."""
        left, top, right, bottom = box
        match self.rotation:
            case 90:
                return top, self.height - right, bottom, self.height - left
            case 180:
                return (
                    self.width - right,
                    self.height - bottom,
                    self.width - left,
                    self.height - top,
                )
            case 270:
                return self.width - bottom, left, self.width - top, right
            case _:
                return box

    def _paint(
        self, line: TextLine, *, origins: list[Point], raster_size: tuple[int, int]
    ) -> list[Paint]:
        _, top, _, bottom = line.box
        size = (bottom - top) / HELVETICA_HEIGHT
        if size <= 0 or len(line.text) > MAX_RAW_TEXT or not line.text.isprintable():
            return []
        try:
            text = line.text.replace(" ", "").encode("cp1252")
        except UnicodeEncodeError:
            return []
        left, upper, right, lower = _crop(line.region, rotation=0, size=raster_size)
        width, height = raster_size
        points = sorted(
            (x, y)
            for x, y in origins
            if left <= floor(x / self.width * width) < right
            and upper <= floor(y / self.height * height) < lower
        )
        if len(text) != len(points):
            return []
        span = source_unit_span(line.text)
        start = end = 0
        if span is not None:
            start = len(line.text[: span[0]].replace(" ", ""))
            end = start + span[1] - span[0]
        return [
            (
                index - start + 1 if start <= index < end else 0,
                f"BT /F1 {size:.6f} Tf 1 0 0 1 {x:.6f} {self.height - y:.6f} Tm "
                f"<{char:02x}> Tj ET",
            )
            for index, (char, (x, y)) in enumerate(zip(text, points, strict=True))
        ]

    def reference(
        self,
        *,
        lines: Iterable[TextLine],
        points: list[Point],
        display_size: tuple[int, int],
    ) -> Reference | None:
        """Paint each unit glyph separately as well as in the full reference."""
        origins = [
            self.unrotate((x, y, x, y))[:2]
            for x, y in points
            if isfinite(x) and isfinite(y)
        ]
        raster_size = display_size[::-1] if self.rotation in (90, 270) else display_size
        paint = [
            glyph
            for line in lines
            for glyph in self._paint(line, origins=origins, raster_size=raster_size)
        ]
        if not paint:
            return None
        last = max(layer for layer, _ink in paint)
        streams = ["\n".join(ink for _layer, ink in paint)]
        if last:
            streams.extend(
                "\n".join(ink for slot, ink in paint if slot == layer)
                for layer in range(last + 1)
            )
        return Reference(self._pdf(streams), len(streams))

    def _pdf(self, streams: list[str]) -> bytes:
        kids = " ".join(f"{i} 0 R" for i in range(4, 4 + 2 * len(streams), 2))
        objects = [
            "<< /Type /Catalog /Pages 2 0 R >>",
            f"<< /Type /Pages /Kids [{kids}] /Count {len(streams)} >>",
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
            "/Encoding /WinAnsiEncoding >>",
        ]
        for index, stream in enumerate(streams):
            objects.extend(
                (
                    f"<< /Type /Page /Parent 2 0 R /Rotate {self.rotation} "
                    f"/MediaBox [0 0 {self.width:.6f} {self.height:.6f}] "
                    "/Resources << /Font << /F1 3 0 R >> >> "
                    f"/Contents {5 + 2 * index} 0 R >>",
                    f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
                )
            )
        pdf = bytearray(b"%PDF-1.4\n")
        xref = ["0000000000 65535 f \n"]
        for index, obj in enumerate(objects, start=1):
            xref.append(f"{len(pdf):010d} 00000 n \n")
            pdf.extend(f"{index} 0 obj\n{obj}\nendobj\n".encode("ascii"))
        pdf.extend(
            (
                f"xref\n0 {len(xref)}\n{''.join(xref)}trailer\n"
                f"<< /Size {len(xref)} /Root 1 0 R >>\nstartxref\n{len(pdf)}\n%%EOF\n"
            ).encode("ascii")
        )
        return bytes(pdf)


def _crop(region: Region, *, rotation: int, size: tuple[int, int]) -> Crop:
    width, height = size
    left, top, right, bottom = region.display_bbox(rotation)
    return (
        floor(left * width / 10000) - INK_MARGIN,
        floor(top * height / 10000) - INK_MARGIN,
        ceil(right * width / 10000) + INK_MARGIN,
        ceil(bottom * height / 10000) + INK_MARGIN,
    )


def _overlap(a: Crop, b: Crop) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _unit_boxes(*, layers: Iterable[Image], box: Crop) -> list[Crop]:
    width, height = box[2] - box[0], box[3] - box[1]
    seen = RasterImage.new("L", (width, height))
    cores = RasterImage.new("L", (width, height))
    boxes: list[Crop] = []
    for layer in layers:
        ink = ImageChops.invert(layer.crop(box))
        core = ink.point(INK_CORE)
        # Adjacent glyphs can share an antialias fringe without touching any
        # foreground stroke. No other ink may enter a glyph's foreground.
        if (
            ImageChops.darker(cores, ink).getbbox() is not None
            or ImageChops.darker(seen, core).getbbox() is not None
        ):
            return []
        bounds = core.getbbox()
        if bounds is not None:
            boxes.append(bounds)
        seen = ImageChops.lighter(seen, ink)
        cores = ImageChops.lighter(cores, core)
    return boxes


def _legible_unit(
    text: str, *, box: Crop, layers: tuple[Image, ...], rotation: int
) -> bool:
    span = source_unit_span(text)
    if span is None:
        return True
    # Layer zero is the rest of the line, so a unit cannot borrow its ink.
    boxes = _unit_boxes(layers=layers, box=box)
    if len(boxes) != 1 + span[1] - span[0]:
        return False
    axis = 1 if rotation in (90, 270) else 0
    unit = boxes[1:] if rotation in (0, 90) else boxes[:0:-1]
    height = max(bounds[3 - axis] - bounds[1 - axis] for bounds in unit)
    return height >= MIN_STROKE_HEIGHT and all(
        left[axis] < right[axis]
        and right[axis] - left[axis + 2] <= height // GAP_HEIGHT_DIVISOR
        for left, right in pairwise(unit)
    )


def matching_ink(
    *,
    source: Image,
    reference: ReferenceInk,
    lines: list[TextLine],
    projected: list[TextLine],
    rotation: int,
) -> dict[Region, str]:
    """Keep exact, unclipped crops on which independent paint reproduces the source."""
    if (
        source.size != reference.page.size
        or source.mode != "RGB"
        or reference.page.mode != "RGB"
    ):
        return {}
    boxes = [
        (line, _crop(line.region, rotation=rotation, size=source.size))
        for line in projected
    ]
    claimed = [
        (_crop(line.region, rotation=rotation, size=source.size), line)
        for line in lines
    ]
    claims = Counter(box for box, _line in claimed)
    originals = dict(claimed)
    proven: dict[Region, str] = {}
    for index, (line, box) in enumerate(boxes):
        # Do not let a neighbouring reference line supply pixels for this one.
        original = originals.get(box)
        if (
            original is None
            or claims[box] != 1
            or original.text != line.text
            or not 0 <= box[0] < box[2] <= source.width
            or not 0 <= box[1] < box[3] <= source.height
            or any(
                _overlap(box, other) for i, (_, other) in enumerate(boxes) if i != index
            )
        ):
            continue
        expected = reference.page.crop(box)
        if (
            ImageChops.invert(expected).getbbox() is not None
            and expected.tobytes() == source.crop(box).tobytes()
            and _legible_unit(
                original.text, box=box, layers=reference.layers, rotation=rotation
            )
        ):
            proven[original.region] = original.text
    return proven
