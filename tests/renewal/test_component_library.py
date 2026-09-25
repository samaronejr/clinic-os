"""Component library contracts that do not need a browser.

Chart geometry, the SC-8 state vocabulary, the showcase matrix, and the
static rules for component scripts (strict plain scripts, no storage).
"""

from __future__ import annotations

import itertools
import re
from decimal import Decimal
from pathlib import Path
from typing import Final

import pytest
from apps.core.charts import (
    MARKERS,
    ChartDataError,
    display_number,
    marker_path,
    trend_geometry,
)
from apps.core.templatetags.components import (
    COMPONENT_STATES,
    PRIMITIVE_EXTRA_STATES,
    diff_counts,
)
from apps.identity.showcase_data import DIFF_LINES, showcase_fixture
from django.template.loader import render_to_string
from django.utils.translation import gettext

ROOT: Final = Path(__file__).resolve().parents[2]
COMPONENT_JS: Final = ROOT / "static" / "js" / "components"
SC8_STATES: Final = (
    "default",
    "hover",
    "focus",
    "active",
    "disabled",
    "loading",
    "error",
    "success",
    "empty",
    "stale",
    "conflict",
    "permission-denied",
    "offline",
)
COMPONENTS: Final = (
    "dialog",
    "drawer",
    "combobox",
    "datetime",
    "resource_grid",
    "command_palette",
    "toast",
    "tabs",
    "segmented",
    "editor",
    "provenance",
    "citation",
    "diff",
    "document_viewer",
    "chart",
)
PRIMITIVES: Final = ("navigation", "field", "action", "status", "table", "panel")
SPEC: Final = re.compile(r'data-primitive="([a-z_]+)" data-state="([a-z-]+)"')


def test_state_vocabulary_is_exactly_sc8_in_order() -> None:
    assert tuple(state.key for state in COMPONENT_STATES) == SC8_STATES
    assert {state.key for state in PRIMITIVE_EXTRA_STATES} == {
        "hover",
        "active",
        "empty",
        "stale",
        "conflict",
        "permission-denied",
        "offline",
    }
    for state in COMPONENT_STATES:
        assert str(state.label) == gettext(str(state.label))


def test_showcase_renders_every_component_and_primitive_in_every_state() -> None:
    html = render_to_string("identity/showcase.html")
    pairs = SPEC.findall(html)
    assert len(pairs) == len(set(pairs)), "duplicate specimen"
    expected = {
        (name, state) for name in (*COMPONENTS, *PRIMITIVES) for state in SC8_STATES
    }
    assert set(pairs) == expected
    # Every specimen renders the component itself, not only its heading.
    for chunk in html.split('<article class="spec" data-primitive=')[1:]:
        body = chunk.split("</article>", 1)[0]
        after_heading = body.split("</h3>", 1)[1]
        assert "<" in after_heading.strip(), chunk[:60]
    ids = re.findall(r'\sid="([^"]+)"', html)
    duplicates = sorted({value for value in ids if ids.count(value) > 1})
    assert duplicates == []
    assert "otpauth://" not in html
    assert "vendor/axe" not in html
    for script in sorted(COMPONENT_JS.glob("*.js")):
        assert f"js/components/{script.name}" in html, script.name


def test_component_scripts_are_strict_plain_and_storage_free() -> None:
    scripts = sorted(COMPONENT_JS.glob("*.js"))
    assert {path.stem for path in scripts} == {
        "announcer",
        "combobox",
        "command-palette",
        "datetime",
        "dialog",
        "document-viewer",
        "drawer",
        "editor-shell",
        "resource-grid",
        "tabs",
    }
    for path in scripts:
        source = path.read_text(encoding="utf-8")
        assert '"use strict";' in source, path.name
        for banned in (
            "localStorage",
            "sessionStorage",
            "indexedDB",
            "caches.",
            "history.pushState",
            "import ",
            "export ",
            "eval(",
            "new Function",
        ):
            assert banned not in source, (path.name, banned)


def test_trend_geometry_scales_values_marks_range_and_builds_the_table() -> None:
    geometry = trend_geometry(
        [
            {"name": "Hb", "values": ["11.4", "12.1", None, "16.5"]},
            {"name": "Ht", "values": ["35", "36", "37", "38"]},
        ],
        labels=["01", "02", "03", "04"],
        ref_low="12",
        ref_high="16",
    )
    first, second = geometry.series
    assert (first.index, first.marker, second.marker) == (1, MARKERS[0], MARKERS[1])
    assert len(first.points) == 3
    assert [point.out_of_range for point in first.points] == [True, False, True]
    assert geometry.out_of_range_count == 2 + 4
    ys = [point.y for point in second.points]
    assert ys == sorted(ys, reverse=True)
    assert geometry.plot_left <= first.points[0].x < first.points[-1].x
    assert geometry.reference is not None
    assert geometry.reference.label == "12-16"
    assert geometry.rows[2].values == ("", "37")
    assert geometry.rows[0].values == ("11,4", "35")
    labels = [Decimal(tick.label.replace(",", ".")) for tick in geometry.ticks]
    steps = {b - a for a, b in itertools.pairwise(labels)}
    assert len(steps) == 1
    assert labels[0] <= Decimal("11.4")
    assert labels[-1] >= Decimal(38)


def test_single_point_and_one_sided_ranges_still_draw() -> None:
    geometry = trend_geometry(
        [{"name": "PA", "values": ["120"]}], labels=["hoje"], ref_high="130"
    )
    assert (
        geometry.series[0].points[0].x == (geometry.plot_left + geometry.plot_right) / 2
    )
    assert geometry.reference is not None
    assert geometry.reference.label == "130"
    flat = trend_geometry([{"name": "x", "values": ["0", "0"]}], labels=["a", "b"])
    assert flat.reference is None
    assert len({tick.y for tick in flat.ticks}) == len(flat.ticks)


@pytest.mark.parametrize(
    ("series", "labels", "bounds"),
    [
        ([], ["a"], {}),
        ([{"name": "x", "values": ["1"]}] * 7, ["a"], {}),
        ([{"name": "x", "values": ["1", "2"]}], ["a"], {}),
        ([{"name": "x", "values": ["abc"]}], ["a"], {}),
        ([{"name": "x", "values": ["NaN"]}], ["a"], {}),
        ([{"name": "x", "values": [None]}], ["a"], {}),
        ([{"name": "x", "values": ["1"]}], [], {}),
        ([{"name": "x", "values": ["1"]}], ["a"], {"ref_low": "5", "ref_high": "1"}),
    ],
)
def test_malformed_chart_input_is_refused(
    series: list[dict[str, object]],
    labels: list[str],
    bounds: dict[str, str],
) -> None:
    with pytest.raises(ChartDataError):
        trend_geometry(series, labels=labels, **bounds)


def test_markers_are_distinct_closed_paths_and_numbers_keep_precision() -> None:
    paths = {marker_path(shape, 10, 10) for shape in MARKERS}
    assert len(paths) == len(MARKERS)
    assert all(path.endswith("Z") for path in paths)
    assert display_number(Decimal("13.20")) == "13,20"
    assert display_number(Decimal(6400)) == "6400"


def test_diff_counts_and_fixtures_fail_loudly() -> None:
    assert showcase_fixture("diff_lines") is DIFF_LINES
    assert diff_counts(DIFF_LINES) == {"added": 1, "removed": 1}
    with pytest.raises(KeyError):
        showcase_fixture("real_patients")


def test_chart_include_renders_svg_and_an_equivalent_table() -> None:
    html = render_to_string(
        "includes/components/chart.html",
        {
            "id": "c",
            "title": "Hb",
            "labels": ["01", "02"],
            "series": [{"name": "Hb", "values": ["12.5", "13"]}],
            "ref_low": "12",
            "ref_high": "16",
            "summary": "Resumo",
        },
    )
    assert 'role="img"' in html
    assert re.search(r'points="[0-9.]+,[0-9.]+ [0-9.]+,[0-9.]+"', html)
    assert "<td>12,5</td>" in html
    assert "<td>13</td>" in html
    assert 'class="chart-range"' in html
    loading = render_to_string(
        "includes/components/chart.html",
        {"id": "c", "title": "Hb", "state": "loading"},
    )
    assert '<svg class="chart-svg"' not in loading
    assert 'aria-busy="true"' in loading
