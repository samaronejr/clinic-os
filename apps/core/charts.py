"""Pure SVG geometry for server-rendered trend charts.

Charts are drawn on the server so they work without JavaScript and always
ship a table alternative built from the same rows. Colors never live here:
series carry an index (1-6) that CSS maps to the ``--viz-n`` tokens, and each
series also has its own marker shape so color is never the only carrier.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from typing import Final

from django.utils.formats import number_format

MAX_SERIES: Final = 6
MARKERS: Final = ("circle", "square", "triangle", "diamond", "triangle-down", "bar")
VIEW_WIDTH: Final = 400
VIEW_HEIGHT: Final = 200
PAD_LEFT: Final = 40
PAD_RIGHT: Final = 20
PAD_TOP: Final = 12
PAD_BOTTOM: Final = 30
MARKER_RADIUS: Final = 4.5
TICK_COUNT: Final = 4
HEADROOM: Final = Decimal("0.1")


class ChartDataError(ValueError):
    """Reject chart input that cannot be drawn honestly."""


@dataclass(frozen=True)
class ChartPoint:
    """One plotted value with its display text."""

    x: float
    y: float
    value: str
    out_of_range: bool
    marker_path: str


@dataclass(frozen=True)
class ChartSeries:
    """One series: line path, markers and legend metadata."""

    index: int
    name: str
    marker: str
    line_points: str
    points: tuple[ChartPoint, ...]
    legend_path: str


@dataclass(frozen=True)
class ChartTick:
    """A labelled horizontal gridline."""

    y: float
    label: str


@dataclass(frozen=True)
class ChartLabel:
    """A labelled x position."""

    x: float
    label: str


@dataclass(frozen=True)
class ChartRow:
    """One table-alternative row: the x label and each series' value."""

    label: str
    values: tuple[str, ...]


@dataclass(frozen=True)
class ChartRange:
    """The reference band rectangle and its readable label."""

    x: float
    y: float
    width: float
    height: float
    label: str


@dataclass(frozen=True)
class TrendGeometry:
    """Everything the chart include needs to draw SVG and its table."""

    width: int
    height: int
    plot_left: float
    plot_right: float
    plot_bottom: float
    series: tuple[ChartSeries, ...]
    ticks: tuple[ChartTick, ...]
    x_labels: tuple[ChartLabel, ...]
    rows: tuple[ChartRow, ...]
    reference: ChartRange | None
    out_of_range_count: int


def _decimal(raw: object) -> Decimal:
    try:
        value = Decimal(str(raw))
    except InvalidOperation as error:
        msg = "chart values must be decimal numbers"
        raise ChartDataError(msg) from error
    if not value.is_finite():
        msg = "chart values must be finite"
        raise ChartDataError(msg)
    return value


def _cell(value: Decimal | None) -> str:
    return "" if value is None else display_number(value)


def display_number(value: Decimal) -> str:
    """Format a decimal for pt-BR display without changing its precision."""
    exponent = value.as_tuple().exponent
    places = -exponent if isinstance(exponent, int) and exponent < 0 else 0
    return str(number_format(value, decimal_pos=places))


def marker_path(shape: str, x: float, y: float, r: float = MARKER_RADIUS) -> str:
    """Return a filled SVG path for one marker shape centered on (x, y)."""
    paths = {
        "circle": (
            f"M{x - r:.1f} {y:.1f}a{r:.1f} {r:.1f} 0 1 0 {2 * r:.1f} 0"
            f"a{r:.1f} {r:.1f} 0 1 0 {-2 * r:.1f} 0Z"
        ),
        "square": f"M{x - r:.1f} {y - r:.1f}h{2 * r:.1f}v{2 * r:.1f}h{-2 * r:.1f}Z",
        "triangle": (
            f"M{x:.1f} {y - r:.1f}L{x + r:.1f} {y + r:.1f}L{x - r:.1f} {y + r:.1f}Z"
        ),
        "diamond": (
            f"M{x:.1f} {y - r:.1f}L{x + r:.1f} {y:.1f}L{x:.1f} {y + r:.1f}"
            f"L{x - r:.1f} {y:.1f}Z"
        ),
        "triangle-down": (
            f"M{x - r:.1f} {y - r:.1f}L{x + r:.1f} {y - r:.1f}L{x:.1f} {y + r:.1f}Z"
        ),
        "bar": f"M{x - r:.1f} {y - r / 2:.1f}h{2 * r:.1f}v{r:.1f}h{-2 * r:.1f}Z",
    }
    return paths[shape]


def _nice_step(span: Decimal) -> Decimal:
    raw = span / TICK_COUNT
    magnitude = Decimal(10) ** raw.adjusted()
    for factor in (1, 2, 5, 10):
        step = magnitude * factor
        if step >= raw:
            return step
    return magnitude * 10  # pragma: no cover - factor 10 always satisfies raw


def _bounds(
    values: Sequence[Decimal], ref_low: Decimal | None, ref_high: Decimal | None
) -> tuple[Decimal, Decimal, Decimal]:
    candidates = [*values]
    candidates.extend(bound for bound in (ref_low, ref_high) if bound is not None)
    low, high = min(candidates), max(candidates)
    span = high - low
    if span == 0:
        span = abs(high) or Decimal(1)
    low, high = low - span * HEADROOM, high + span * HEADROOM
    step = _nice_step(high - low)
    return (
        (low / step).to_integral_value(ROUND_FLOOR) * step,
        (high / step).to_integral_value(ROUND_CEILING) * step,
        step,
    )


@dataclass(frozen=True)
class _Scale:
    """Map label positions and values into the plot box."""

    count: int
    low: Decimal
    high: Decimal
    ref_low: Decimal | None
    ref_high: Decimal | None
    left: float = float(PAD_LEFT)
    right: float = float(VIEW_WIDTH - PAD_RIGHT)
    top: float = float(PAD_TOP)
    bottom: float = float(VIEW_HEIGHT - PAD_BOTTOM)

    def x_at(self, index: int) -> float:
        if self.count == 1:
            return (self.left + self.right) / 2
        return self.left + (self.right - self.left) / (self.count - 1) * index

    def y_at(self, value: Decimal) -> float:
        ratio = float((value - self.low) / (self.high - self.low))
        return self.bottom - ratio * (self.bottom - self.top)

    def outside(self, value: Decimal) -> bool:
        return (self.ref_low is not None and value < self.ref_low) or (
            self.ref_high is not None and value > self.ref_high
        )


def _parse(
    series: Sequence[Mapping[str, object]], labels: Sequence[str]
) -> list[tuple[str, list[Decimal | None]]]:
    if not labels:
        msg = "a trend needs at least one x label"
        raise ChartDataError(msg)
    if not series or len(series) > MAX_SERIES:
        msg = "a trend draws between one and six series"
        raise ChartDataError(msg)
    parsed: list[tuple[str, list[Decimal | None]]] = []
    for entry in series:
        raw_values = entry.get("values")
        if not isinstance(raw_values, Sequence) or len(raw_values) != len(labels):
            msg = "each series needs one value (or None) per label"
            raise ChartDataError(msg)
        parsed.append(
            (
                str(entry.get("name", "")),
                [None if raw is None else _decimal(raw) for raw in raw_values],
            )
        )
    return parsed


def _draw(
    index: int, name: str, values: list[Decimal | None], scale: _Scale
) -> ChartSeries:
    shape = MARKERS[index]
    points: list[ChartPoint] = []
    for position, value in enumerate(values):
        if value is None:
            continue
        x, y = scale.x_at(position), scale.y_at(value)
        points.append(
            ChartPoint(
                x=round(x, 1),
                y=round(y, 1),
                value=display_number(value),
                out_of_range=scale.outside(value),
                marker_path=marker_path(shape, x, y),
            )
        )
    return ChartSeries(
        index=index + 1,
        name=name,
        marker=shape,
        line_points=" ".join(f"{p.x:.1f},{p.y:.1f}" for p in points),
        points=tuple(points),
        legend_path=marker_path(shape, 12.0, 6.0, 4.0),
    )


def _reference(scale: _Scale) -> ChartRange | None:
    if scale.ref_low is None and scale.ref_high is None:
        return None
    top_value = scale.ref_high if scale.ref_high is not None else scale.high
    bottom_value = scale.ref_low if scale.ref_low is not None else scale.low
    top, bottom = scale.y_at(top_value), scale.y_at(bottom_value)
    if scale.ref_low is not None and scale.ref_high is not None:
        label = f"{display_number(bottom_value)}-{display_number(top_value)}"
    else:
        label = display_number(bottom_value if scale.ref_low is not None else top_value)
    return ChartRange(
        x=scale.left,
        y=round(top, 1),
        width=round(scale.right - scale.left, 1),
        height=round(bottom - top, 1),
        label=label,
    )


def trend_geometry(
    series: Sequence[Mapping[str, object]],
    *,
    labels: Sequence[str],
    ref_low: object = None,
    ref_high: object = None,
) -> TrendGeometry:
    """Compute the SVG geometry for up to six series sharing ``labels``."""
    parsed = _parse(series, labels)
    low_ref = None if ref_low is None else _decimal(ref_low)
    high_ref = None if ref_high is None else _decimal(ref_high)
    if low_ref is not None and high_ref is not None and low_ref > high_ref:
        msg = "reference range is inverted"
        raise ChartDataError(msg)
    present = [value for _, values in parsed for value in values if value is not None]
    if not present:
        msg = "a trend needs at least one value"
        raise ChartDataError(msg)
    low, high, tick_step = _bounds(present, low_ref, high_ref)
    scale = _Scale(
        count=len(labels), low=low, high=high, ref_low=low_ref, ref_high=high_ref
    )
    drawn = tuple(
        _draw(index, name, values, scale) for index, (name, values) in enumerate(parsed)
    )
    ticks: list[ChartTick] = []
    value = low
    while value <= high:
        ticks.append(
            ChartTick(y=round(scale.y_at(value), 1), label=display_number(value))
        )
        value += tick_step
    rows = tuple(
        ChartRow(
            label=label,
            values=tuple(_cell(values[position]) for _, values in parsed),
        )
        for position, label in enumerate(labels)
    )
    return TrendGeometry(
        width=VIEW_WIDTH,
        height=VIEW_HEIGHT,
        plot_left=scale.left,
        plot_right=scale.right,
        plot_bottom=scale.bottom,
        series=drawn,
        ticks=tuple(ticks),
        x_labels=tuple(
            ChartLabel(x=round(scale.x_at(position), 1), label=label)
            for position, label in enumerate(labels)
        ),
        rows=rows,
        reference=_reference(scale),
        out_of_range_count=sum(
            point.out_of_range for line in drawn for point in line.points
        ),
    )
