"""Exact review-pixel verification of proposals in the synthetic PDF format."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from PIL import Image as RasterImage
from PIL import ImageChops

from apps.ai.extraction_contracts import (
    COORDINATE_SCALE,
    ExtractionInputError,
    source_unit_span,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from PIL.Image import Image

    from apps.ai.extraction_contracts import Region

type Box = tuple[float, float, float, float]
type Pixel = tuple[int, int, int]

REVIEW_LONG_EDGE = 1400
_ROTATIONS = (0, 90, 180, 270)
_PHASES = 4
_CELL_WIDTH = 32
_CELL_HEIGHT = 48
_ATLAS_COLUMNS = 32
_BASELINE = 42
_START_MARKER = 13
_GLYPH_START = 19
_FONT_SIZE = 12
_PAGE = (612, 792)
_REVIEW_SCALE = REVIEW_LONG_EDGE / _PAGE[1]
_FOREGROUND = 128

# The box identifies the row, not a source-metadata word or glyph position.
# Include visible continuation in the band rather than trusting a truncated box.
_BEARING = 3
_CONTINUATION = 7
_MAX_BAND_HEIGHT = 32
_MAX_GLYPHS = 256
_MAX_PLACEMENTS = 32000
# Consecutive independently positioned glyphs can straddle opposite hinting
# phases. Try both raster baselines; their coverage must still match exactly.
_BASELINE_DRIFT = 2
_UNIT_GAP = 6
_WORD_GAP = 18
_WORD_SPACE_MIN = 4
_INTRAWORD_DRIFT = 3


def _reference_pdf(stream: str, rotation: int) -> bytes:
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [4 0 R] /Count 1 >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
        "/Encoding /WinAnsiEncoding >>",
        f"<< /Type /Page /Parent 2 0 R /Rotate {rotation} "
        f"/MediaBox [0 0 {_PAGE[0]} {_PAGE[1]}] "
        "/Resources << /Font << /F1 3 0 R >> >> /Contents 5 0 R >>",
        f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
    ]
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


@dataclass(frozen=True, slots=True)
class _Glyph:
    width: int
    height: int
    top: int  # Relative to the glyph's baseline.
    origin: int  # Offset from the leftmost ink to the origin marker.
    advance: int  # Raster distance between that marker and the next origin.
    ink: tuple[Pixel, ...]

    @classmethod
    def from_cell(cls, cell: Image) -> _Glyph | None:
        end = cell.crop((0, 0, _CELL_WIDTH, _START_MARKER)).getbbox()
        start = cell.crop((0, _START_MARKER, _CELL_WIDTH, _GLYPH_START)).getbbox()
        glyph = cell.crop((0, _GLYPH_START, _CELL_WIDTH, _CELL_HEIGHT))
        box = glyph.getbbox()
        if box is None or start is None or end is None:
            return None
        left, top, right, bottom = box
        width, height = right - left, bottom - top
        return cls(
            width,
            height,
            top + _GLYPH_START - _BASELINE,
            start[0] - left,
            end[0] - start[0],
            tuple(
                (index % width, index // width, darkness)
                for index, darkness in enumerate(glyph.crop(box).tobytes())
                if darkness
            ),
        )


type GlyphAtlas = dict[str, dict[_Glyph, int]]


def _atlas_chars() -> tuple[tuple[int, str], ...]:
    chars = []
    for code in range(33, 256):
        char = bytes((code,)).decode("cp1252", errors="ignore")
        if char.isprintable():
            chars.append((code, char))
    return tuple(chars)


def _atlas_pdf(
    chars: tuple[tuple[int, str], ...], rotation: int
) -> tuple[bytes, list[tuple[str, int, int]]]:
    paint: list[str] = []
    cells: list[tuple[str, int, int]] = []
    for index, (code, char) in enumerate(chars):
        for phase in range(_PHASES):
            slot = index * _PHASES + phase
            left = slot % _ATLAS_COLUMNS * _CELL_WIDTH
            top = slot // _ATLAS_COLUMNS * _CELL_HEIGHT
            # Splash samples one device axis at a quarter-pixel phase. Include
            # every phase on an otherwise integer device grid, independent of
            # the source PDF and its renderer coordinate rounding.
            fraction = (phase + 0.5) / _PHASES
            horizontal = rotation in (0, 180)
            x = (left + 4 + (fraction if horizontal else 0)) / _REVIEW_SCALE
            y = _PAGE[1] - (top + _BASELINE + (0 if horizontal else fraction)) / (
                _REVIEW_SCALE
            )
            # Raised dots locate the origin and advance without affecting the
            # glyph's ink mask or consulting font metrics in the source PDF.
            origin = f"BT /F1 {_FONT_SIZE} Tf 0 Ts 1 0 0 1 {x:.8f} {y:.8f} Tm"
            paint.extend(
                (
                    f"{origin} <{code:02x}> Tj 18.5 Ts (.) Tj ET",
                    f"{origin} 14.5 Ts (.) Tj ET",
                )
            )
            cells.append((char, left, top))
    return _reference_pdf("\n".join(paint), rotation), cells


def glyph_atlas(render: Callable[[bytes], Image]) -> GlyphAtlas:
    """Render a fixed black Helvetica atlas; never learn glyphs from source PDFs."""
    chars = _atlas_chars()
    atlas: GlyphAtlas = {}
    for family, rotation in enumerate(_ROTATIONS):
        pdf, cells = _atlas_pdf(chars, rotation)
        raster = render(pdf)
        if raster.mode != "RGB":
            raise ExtractionInputError
        upright = raster.rotate(rotation, expand=True)
        ink = ImageChops.invert(upright.getchannel("R"))
        for char, x, y in cells:
            glyph = _Glyph.from_cell(
                ink.crop((x, y, x + _CELL_WIDTH, y + _CELL_HEIGHT))
            )
            if glyph is not None:
                choices = atlas.setdefault(char, {})
                choices[glyph] = choices.get(glyph, 0) | (1 << family)
    if not atlas:
        raise ExtractionInputError
    return atlas


@dataclass(frozen=True, slots=True)
class _TokenGlyph:
    char: str
    starts_word: bool
    unit: bool


def _characters(text: str) -> tuple[_TokenGlyph, ...]:
    span = source_unit_span(text)
    return tuple(
        _TokenGlyph(
            char, i > 0 and text[i - 1] == " ", bool(span and span[0] <= i < span[1])
        )
        for i, char in enumerate(text)
        if char != " "
    )


@dataclass(frozen=True, slots=True)
class _Band:
    ink: bytes  # Column-major darkness, for comparing progressively placed glyphs.
    height: int
    left: int
    right: int

    @property
    def width(self) -> int:
        return len(self.ink) // self.height

    @classmethod
    def from_box(cls, image: Image, *, region: Region, rotation: int) -> _Band | None:
        width, height = image.size if rotation in (0, 180) else image.size[::-1]
        bbox = region.display_bbox(-rotation % 360)
        x0, y0, x1, y1 = (
            (c * side + COORDINATE_SCALE // 2) // COORDINATE_SCALE
            for c, side in zip(bbox, (width, height, width, height), strict=True)
        )
        if not 0 < y1 - y0 <= _MAX_BAND_HEIGHT or x1 <= x0:
            return None
        left, top = max(0, x0 - _CONTINUATION), max(0, y0 - _BEARING)
        right = min(width, x1 + _CONTINUATION)
        bottom = min(height, y1 + _BEARING)
        # Map the small upright band back into the original display pixels.
        # A transpose rotates exactly; no interpolation enters the proof.
        match rotation:
            case 90:
                crop = (height - bottom, left, height - top, right)
            case 180:
                crop = (width - right, height - bottom, width - left, height - top)
            case 270:
                crop = (top, width - right, bottom, width - left)
            case _:
                crop = (left, top, right, bottom)
        red, green, blue = image.crop(crop).rotate(rotation, expand=True).split()
        if (
            ImageChops.difference(red, green).getbbox()
            or ImageChops.difference(green, blue).getbbox()
        ):
            return None
        ink = ImageChops.invert(red).transpose(RasterImage.Transpose.TRANSPOSE)
        return cls(ink.tobytes(), ink.width, x0 - left, x1 - left)


@dataclass(frozen=True, slots=True)
class _Placement:
    left: int
    right: int
    baseline: int
    next_origin: int
    family: int


class _Matcher:
    def __init__(
        self, *, band: _Band, text: tuple[_TokenGlyph, ...], atlas: GlyphAtlas
    ) -> None:
        self.band = band
        self.text = text
        self.atlas = atlas
        self.placed = bytearray(len(band.ink))
        self.owners = [-1] * len(band.ink)
        self.attempts = 0

    def _changes(
        self, glyph: _Glyph, *, index: int, x: int, y: int
    ) -> list[tuple[int, int]] | None:
        changes = []
        for dx, dy, darkness in glyph.ink:
            pos = (x + dx) * self.band.height + y + dy
            prior = self.placed[pos]
            ink = darkness
            if prior:
                owner = self.owners[pos]
                if (
                    self.text[owner].unit != self.text[index].unit
                    or index - owner != 1
                    or self.text[index].starts_word
                ):
                    return None
                # Exact opaque-black coverage composition, including edges.
                ink = 255 - ((255 - prior) * (255 - ink) + 127) // 255
                if ink >= _FOREGROUND:
                    return None
            if ink > self.band.ink[pos]:
                return None
            changes.append((pos, ink))
        return changes

    def _cursor(self, first: int) -> int:
        height, ink = self.band.height, self.band.ink
        for pos in range(first * height, len(ink), height):
            if self.placed[pos : pos + height] != ink[pos : pos + height]:
                return pos // height
        return self.band.width

    def _locations(
        self, glyph: _Glyph, *, cursor: int, index: int, previous: _Placement | None
    ) -> tuple[range, range]:
        if previous is None:
            return range(cursor, cursor + 1), range(self.band.height - glyph.height + 1)
        token = self.text[index]
        low = max(previous.left + 1, cursor - _BEARING)
        high = cursor
        expected = previous.next_origin - glyph.origin
        if token.starts_word:
            low = max(low, expected + _WORD_SPACE_MIN)
            high = min(high, expected + _WORD_GAP)
        elif token.unit:
            high = min(high, previous.right + _UNIT_GAP)
        else:
            # The word boundary must be visible. A metadata-only split in a
            # digit or label is allowed; a printed space in it is not.
            low = max(low, expected - _INTRAWORD_DRIFT)
            high = min(high, expected + _INTRAWORD_DRIFT)
        top = previous.baseline + glyph.top
        return (
            range(low, high + 1),
            range(
                max(0, top - _BASELINE_DRIFT),
                min(top + _BASELINE_DRIFT, self.band.height - glyph.height) + 1,
            ),
        )

    def _visit(
        self, index: int, placement: _Placement, changes: list[tuple[int, int]]
    ) -> bool:
        undo = []
        for pos, ink in changes:
            undo.append((pos, self.placed[pos]))
            self.placed[pos] = ink
            if self.owners[pos] < 0:
                self.owners[pos] = index
        if self._walk(index + 1, placement):
            return True
        for pos, ink in undo:
            self.placed[pos] = ink
            if not ink:
                self.owners[pos] = -1
        return False

    def _try_glyph(
        self, glyph: _Glyph, *, family: int, index: int, xs: range, ys: range
    ) -> bool:
        for x in xs:
            if x + glyph.width > self.band.width:
                continue
            for y in ys:
                self.attempts += 1
                if self.attempts > _MAX_PLACEMENTS:
                    return False
                changes = self._changes(glyph, index=index, x=x, y=y)
                if changes is not None and self._visit(
                    index,
                    _Placement(
                        x,
                        x + glyph.width,
                        y - glyph.top,
                        x + glyph.origin + glyph.advance,
                        family,
                    ),
                    changes,
                ):
                    return True
        return False

    def _walk(self, index: int, previous: _Placement | None) -> bool:
        cursor = self._cursor(previous.left if previous else 0)
        end = cursor == self.band.width
        if index == len(self.text):
            return (
                end
                and previous is not None
                and abs(previous.right - self.band.right) <= _BEARING
            )
        if end or (previous is None and abs(cursor - self.band.left) > _BEARING):
            return False
        family = previous.family if previous else (1 << len(_ROTATIONS)) - 1
        for glyph, mask in self.atlas[self.text[index].char].items():
            if not mask & family or self.attempts >= _MAX_PLACEMENTS:
                continue
            xs, ys = self._locations(
                glyph, cursor=cursor, index=index, previous=previous
            )
            if self._try_glyph(glyph, family=mask & family, index=index, xs=xs, ys=ys):
                return True
        return False

    def matches(self) -> bool:
        """Require every review sample in the band to be accounted for."""
        return self._walk(0, None)


def matches_proposal(
    image: Image,
    *,
    region: Region,
    text: str,
    atlas: GlyphAtlas,
) -> bool:
    """Match complete glyphs using display pixels, proposed text and a display box."""
    glyphs = _characters(text)
    if not 0 < len(glyphs) <= _MAX_GLYPHS or any(g.char not in atlas for g in glyphs):
        return False
    for rotation in _ROTATIONS:
        band = _Band.from_box(image, region=region, rotation=rotation)
        if band is not None and _Matcher(band=band, text=glyphs, atlas=atlas).matches():
            return True
    return False
