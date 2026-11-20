"""Verify the paint of the bounded synthetic PDF text format, not its text layer."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from html.parser import HTMLParser
from math import ceil, floor, isfinite
from typing import TYPE_CHECKING

from PIL import ImageChops

from apps.ai.extraction_contracts import MAX_RAW_TEXT

if TYPE_CHECKING:
    from collections.abc import Iterable

    from PIL.Image import Image

    from apps.ai.extraction_contracts import Region

type Box = tuple[float, float, float, float]
type Crop = tuple[int, int, int, int]
type Point = tuple[float, float]
MIN_EM_PIXELS = 12
INK_MARGIN = 3
HELVETICA_HEIGHT = 0.925
ORIGIN_ROUNDING = 0.001  # Cairo serializes coordinates in points.


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

    def _paint(self, line: TextLine, *, origins: list[Point], long_edge: int) -> str:
        left, top, right, bottom = line.box
        size = (bottom - top) / HELVETICA_HEIGHT
        if (
            size / max(self.width, self.height) * long_edge < MIN_EM_PIXELS
            or len(line.text) > MAX_RAW_TEXT
            or not line.text.isprintable()
        ):
            return ""
        try:
            text = line.text.replace(" ", "").encode("cp1252")
        except UnicodeEncodeError:
            return ""
        points = sorted(
            (x, y)
            for x, y in origins
            if left - ORIGIN_ROUNDING <= x < right and top <= y <= bottom
        )
        if len(text) != len(points) or len(points) != len(set(points)):
            return ""
        paint = " ".join(
            f"1 0 0 1 {x:.6f} {self.height - y:.6f} Tm <{char:02x}> Tj"
            for char, (x, y) in zip(text, points, strict=True)
        )
        return f"BT /F1 {size:.6f} Tf {paint} ET"

    def reference(
        self, *, lines: Iterable[TextLine], points: list[Point], long_edge: int
    ) -> bytes:
        """Build a PDF with only local black Helvetica paint and no source objects."""
        origins = [self.unrotate((x, y, x, y))[:2] for x, y in points]
        stream = "\n".join(
            filter(
                None,
                (
                    self._paint(line, origins=origins, long_edge=long_edge)
                    for line in lines
                ),
            )
        )
        if not stream:
            return b""
        objects = (
            "<< /Type /Catalog /Pages 2 0 R >>",
            "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            f"<< /Type /Page /Parent 2 0 R /Rotate {self.rotation} "
            f"/MediaBox [0 0 {self.width:.6f} {self.height:.6f}] "
            "/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
            "/Encoding /WinAnsiEncoding >>",
            f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
        )
        pdf = bytearray(b"%PDF-1.4\n")
        xref = ["0000000000 65535 f \n"]
        for index, obj in enumerate(objects, start=1):
            xref.append(f"{len(pdf):010d} 00000 n \n")
            pdf.extend(f"{index} 0 obj\n{obj}\nendobj\n".encode("ascii"))
        pdf.extend(
            (
                f"xref\n0 6\n{''.join(xref)}trailer\n"
                f"<< /Size 6 /Root 1 0 R >>\nstartxref\n{len(pdf)}\n%%EOF\n"
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


def matching_ink(
    *,
    source: Image,
    reference: Image,
    lines: list[TextLine],
    projected: list[TextLine],
    rotation: int,
) -> dict[Region, str]:
    """Keep exact, unclipped crops on which independent paint reproduces the source."""
    if source.size != reference.size or source.mode != "RGB" or reference.mode != "RGB":
        return {}
    boxes = [
        (line, _crop(line.region, rotation=rotation, size=source.size))
        for line in projected
    ]
    claims = Counter(line.region.bbox for line in lines)
    originals = {line.region.bbox: line for line in lines}
    proven: dict[Region, str] = {}
    for index, (line, box) in enumerate(boxes):
        # Do not let a neighbouring reference line supply pixels for this one.
        original = originals.get(line.region.bbox)
        if (
            original is None
            or claims[line.region.bbox] != 1
            or original.text != line.text
            or not 0 <= box[0] < box[2] <= source.width
            or not 0 <= box[1] < box[3] <= source.height
            or any(
                _overlap(box, other) for i, (_, other) in enumerate(boxes) if i != index
            )
        ):
            continue
        expected = reference.crop(box)
        if (
            ImageChops.invert(expected).getbbox() is not None
            and expected.tobytes() == source.crop(box).tobytes()
        ):
            proven[original.region] = original.text
    return proven
