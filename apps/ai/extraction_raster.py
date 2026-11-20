"""Verify the paint of the bounded synthetic PDF text format, not its text layer."""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from itertools import pairwise
from math import isfinite
from typing import TYPE_CHECKING

from PIL import Image as RasterImage
from PIL import ImageChops, ImageFilter

from apps.ai.extraction_contracts import (
    MAX_RAW_TEXT,
    ExtractionInputError,
    source_unit_span,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from PIL.Image import Image

    from apps.ai.extraction_contracts import Region

type Box = tuple[float, float, float, float]
type Point = tuple[float, float]
type Paint = tuple[int, str]
MIN_STROKE_HEIGHT = 8
GAP_HEIGHT_DIVISOR = 3  # At most one third of the tallest unit glyph.
HELVETICA_HEIGHT = 0.925
HELVETICA_DESCENT = 0.207
# Classify ownership at the half-coverage boundary. Keep all nonwhite samples
# in the support mask and check them against the original RGB composite.
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
    """Text and placement hints, none of which is evidence of visible ink."""

    region: Region
    box: Box
    first_word: Box
    text: str

    @property
    def font_size(self) -> float:
        """Use the stable label's height, not the union of displaced glyphs."""
        return (self.first_word[3] - self.first_word[1]) / HELVETICA_HEIGHT

    def origin_distance(self, point: Point) -> float:
        """Associate renderer position hints with the nearest text hypothesis."""
        x, y = point
        left, _, right, _ = self.box
        baseline = self.first_word[3] - self.font_size * HELVETICA_DESCENT
        return abs(y - baseline) + max(left - x, 0, x - right)


@dataclass(frozen=True, slots=True, repr=False)
class InkClaim:
    """An output anchor and its independent stencil count, not a crop."""

    region: Region
    text: str
    unit_glyphs: int


@dataclass(frozen=True, slots=True, repr=False)
class Reference:
    """Canonical composite followed by one stencil group per line."""

    pdf: bytes
    claims: tuple[InkClaim, ...]
    pages: int


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

    def _paint(self, line: TextLine, origins: list[Point]) -> list[Paint]:
        size = line.font_size
        if (
            not isfinite(size)
            or size <= 0
            or len(line.text) > MAX_RAW_TEXT
            or not line.text.isprintable()
        ):
            return []
        try:
            text = line.text.replace(" ", "").encode("cp1252")
        except UnicodeEncodeError:
            return []
        if len(text) != len(origins):
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
            for index, (char, (x, y)) in enumerate(
                zip(text, sorted(origins), strict=True)
            )
        ]

    def reference(
        self, *, lines: list[TextLine], points: list[Point]
    ) -> Reference | None:
        """Use layout hints to paint hypotheses, not to accept or reject ink."""
        origins: list[list[Point]] = [[] for _ in lines]
        for x, y in points:
            if isfinite(x) and isfinite(y):
                point = self.unrotate((x, y, x, y))[:2]
                index = min(
                    range(len(lines)), key=lambda i: lines[i].origin_distance(point)
                )
                origins[index].append(point)

        composite: list[str] = []
        stencils: list[str] = []
        claims: list[InkClaim] = []
        for line, positions in zip(lines, origins, strict=True):
            paint = self._paint(line, positions)
            if not paint:
                continue
            last = max(slot for slot, _command in paint)
            claims.append(InkClaim(line.region, line.text, last))
            composite.extend(command for _slot, command in paint)
            stencils.extend(
                "\n".join(command for slot, command in paint if slot == layer)
                for layer in range(last + 1)
            )
        if not claims:
            return None
        streams = ["\n".join(composite), *stencils]
        return Reference(self._pdf(streams), tuple(claims), len(streams))

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


def _successive(strokes: list[tuple[int, int, int]], *, rotation: int) -> bool:
    if not strokes:
        return False
    if rotation in (180, 270):
        strokes.reverse()
    height = max(stroke[2] for stroke in strokes)
    return height >= MIN_STROKE_HEIGHT and all(
        left[0] < right[0] and right[0] - left[1] <= height // GAP_HEIGHT_DIVISOR
        for left, right in pairwise(strokes)
    )


def _unit_stencils(
    rest: Image, *, glyphs: Iterable[Image], composite: Image, rotation: int
) -> tuple[Image, bool]:
    """Keep the full non-background support and check ownership of each stroke."""
    seen = rest
    nonadjacent = rest
    foreground = rest.point(INK_CORE)
    legible = foreground.getbbox() is not None
    axis = int(rotation in (90, 270))
    strokes: list[tuple[int, int, int]] = []
    for ink in glyphs:
        core = ink.point(INK_CORE)
        # Only adjacent unit glyphs may share faint antialias samples. A
        # neighbouring mark cannot supply ink, even at the edge of a stroke.
        if (
            core.getbbox() is None
            or ImageChops.darker(nonadjacent, ink).getbbox() is not None
            or ImageChops.darker(foreground, ink).getbbox() is not None
            or ImageChops.darker(seen, core).getbbox() is not None
            or ImageChops.darker(ImageChops.darker(seen, ink), composite).getbbox()
            is not None
        ):
            legible = False
        else:
            projections = core.getprojection()
            advance, cross = projections[axis], projections[1 - axis]
            strokes.append(
                (
                    advance.index(1),
                    len(advance) - advance[::-1].index(1),
                    len(cross) - cross[::-1].index(1) - cross.index(1),
                )
            )
        nonadjacent, seen = seen, ImageChops.lighter(seen, ink)
        foreground = ImageChops.lighter(foreground, core)
    return seen, legible and _successive(strokes, rotation=rotation)


def _line_changed(ink: Image, changed: Image, *, rotation: int) -> bool:
    """Compare each scanline between its first and last actual ink samples."""
    if rotation in (90, 270):
        ink = ink.transpose(RasterImage.Transpose.TRANSPOSE)
        changed = changed.transpose(RasterImage.Transpose.TRANSPOSE)
    width = ink.width
    stencil = ink.tobytes()
    delta = changed.tobytes()
    for start in range(0, len(stencil), width):
        scan = stencil[start : start + width].strip(b"\0")
        if scan:
            left = stencil.index(scan[:1], start, start + width)
            if delta[left : left + len(scan)].strip(b"\0"):
                return True
    return False


def matching_ink(
    *,
    source: Image,
    frames: Iterator[Image],
    claims: tuple[InkClaim, ...],
    rotation: int,
) -> dict[Region, str]:
    """Prove pixel support and ownership without consulting the cited box."""
    expected = next(frames)
    red, green, blue = ImageChops.difference(source, expected).split()
    changed = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    # An opaque stroke can hide an intersecting drawing inside the stroke
    # while leaving unexpected ink attached just outside its support.
    contact = changed.filter(ImageFilter.MaxFilter(3)) if changed.getbbox() else None
    foreground = ImageChops.invert(expected.getchannel("R")).point(INK_CORE)
    seen = RasterImage.new("L", source.size)
    overlaps = RasterImage.new("L", source.size)
    eligible = []
    for claim in claims:
        rest = next(frames)
        ink, legible = (
            _unit_stencils(
                rest,
                glyphs=(next(frames) for _ in range(claim.unit_glyphs)),
                composite=foreground,
                rotation=rotation,
            )
            if claim.unit_glyphs
            else (rest, True)
        )
        # The extent only limits work on the mask; extents may overlap or touch
        # the page edge. They never count as paint or as conflicting geometry.
        extent = ink.getbbox()
        if extent is None:
            continue
        tile = ink.crop(extent)
        prior = seen.crop(extent)
        overlaps.paste(
            ImageChops.lighter(overlaps.crop(extent), ImageChops.darker(prior, tile)),
            extent,
        )
        seen.paste(ImageChops.lighter(prior, tile), extent)
        if legible and (
            contact is None
            or (
                ImageChops.darker(tile, contact.crop(extent)).getbbox() is None
                and not _line_changed(tile, changed.crop(extent), rotation=rotation)
            )
        ):
            eligible.append((claim, extent, tile))
    if next(frames, None) is not None:
        raise ExtractionInputError
    return {
        claim.region: claim.text
        for claim, extent, ink in eligible
        if ImageChops.darker(overlaps.crop(extent), ink).getbbox() is None
    }
