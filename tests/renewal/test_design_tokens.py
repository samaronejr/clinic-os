"""Design tokens: parse the token blocks of clinic-os.css and prove the pairs.

Raw values live only in ``:root``, ``[data-theme="dark"]`` and
``[data-density="compact"]``. Every text pair must reach 4.5:1 and every
control boundary, focus ring and data-viz hue 3:1, in light and dark. The six
data-viz hues must stay distinguishable under simulated protanopia,
deuteranopia and tritanopia (Machado et al. 2009, severity 1.0).
"""

from __future__ import annotations

import hashlib
import itertools
import math
import re
from pathlib import Path
from typing import Final

import pytest
from apps.core.views import SHELL_STATIC_ASSETS

ROOT: Final = Path(__file__).resolve().parents[2]
CSS: Final = ROOT / "static" / "css"
SHELL_CSS: Final = CSS / "clinic-os.css"
COMPONENT_CSS: Final = CSS / "clinic-os-components.css"
AXE: Final = ROOT / "static" / "vendor" / "axe"
AXE_SHA256: Final = "c24f097bd2f451d4f933e8bc7d8d539f8672a2ebcb5cc9f9f3eec8ca9470a0c1"
AXE_LICENSE_SHA256: Final = (
    "af175b9d96ee93c21a036152e1b905b0b95304d4ae8c2c921c7609100ba8df7e"
)
TEXT: Final = 4.5
UI: Final = 3.0
CVD_MIN_DELTA_E: Final = 12.0
NORMAL_MIN_DELTA_E: Final = 20.0
BLOCKS: Final = {
    "root": ":root",
    "dark": '[data-theme="dark"]',
    "compact": '[data-density="compact"]',
}
RAW_COLOR: Final = re.compile(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(|\boklch\(")

# (foreground, background, minimum, role) - text pairs need 4.5:1, UI 3:1.
PAIRS: Final = (
    ("--color-ink", "--color-surface", TEXT, "body text on canvas"),
    ("--color-ink", "--color-surface-raised", TEXT, "text on panels"),
    ("--color-ink", "--color-surface-sunken", TEXT, "text on sunken paper"),
    ("--color-ink", "--color-selection", TEXT, "text on selected rows"),
    ("--color-ink-soft", "--color-surface", TEXT, "hints on canvas"),
    ("--color-ink-soft", "--color-surface-raised", TEXT, "hints on panels"),
    ("--color-ink-soft", "--color-surface-sunken", TEXT, "disabled text"),
    ("--color-primary", "--color-surface", TEXT, "links on canvas"),
    ("--color-primary", "--color-surface-raised", TEXT, "links on panels"),
    ("--color-primary-strong", "--color-surface-raised", TEXT, "secondary action"),
    ("--color-primary-strong", "--color-selection", TEXT, "action on selection"),
    ("--color-on-primary", "--color-primary", TEXT, "primary button"),
    ("--color-on-primary", "--color-primary-strong", TEXT, "primary hover"),
    ("--color-success", "--color-success-tint", TEXT, "success notice"),
    ("--color-success", "--color-surface-raised", TEXT, "success text"),
    ("--color-error", "--color-error-tint", TEXT, "error notice"),
    ("--color-error", "--color-surface-raised", TEXT, "error text"),
    ("--color-warning", "--color-warning-tint", TEXT, "warning notice"),
    ("--color-warning", "--color-surface-raised", TEXT, "warning text"),
    ("--color-info", "--color-info-tint", TEXT, "info notice"),
    ("--color-info", "--color-surface-raised", TEXT, "info text"),
    ("--color-primary-strong", "--color-warning-tint", TEXT, "action in warning"),
    ("--color-primary-strong", "--color-error-tint", TEXT, "action in error"),
    ("--color-ink-on-dark", "--color-surface-dark", TEXT, "nav current"),
    ("--color-ink-on-dark-soft", "--color-surface-dark", TEXT, "nav items"),
    ("--color-ink-on-dark-muted", "--color-surface-dark", TEXT, "nav unavailable"),
    ("--color-ink-on-dark", "--color-surface-dark-hover", TEXT, "nav hover"),
    ("--color-on-marker", "--color-success-marker", TEXT, "nav success badge"),
    ("--color-control-border", "--color-surface-raised", UI, "control boundary"),
    ("--color-control-border", "--color-surface", UI, "control on canvas"),
    ("--color-focus", "--color-surface", UI, "focus ring on canvas"),
    ("--color-focus", "--color-surface-raised", UI, "focus ring on panels"),
    ("--color-focus", "--color-selection", UI, "focus ring on selection"),
    ("--color-focus-on-dark", "--color-surface-dark", UI, "focus ring on nav"),
    ("--color-marker", "--color-surface-dark", UI, "current-item bar"),
    ("--color-primary", "--color-surface-raised", UI, "loading border"),
    ("--color-error", "--color-surface-raised", UI, "invalid border"),
    ("--color-warning", "--color-surface-raised", UI, "stale border"),
)
DARK_UNCHANGED: Final = frozenset(
    {"--color-ink-on-dark", "--color-ink-on-dark-soft", "--color-ink-on-dark-muted"}
)
VIZ_SURFACES: Final = ("--color-surface", "--color-surface-raised")
CVD: Final[dict[str, tuple[tuple[float, float, float], ...]]] = {
    "protan": (
        (0.152286, 1.052583, -0.204868),
        (0.114503, 0.786281, 0.099216),
        (-0.003882, -0.048116, 1.051998),
    ),
    "deutan": (
        (0.367322, 0.860646, -0.227968),
        (0.280085, 0.672501, 0.047413),
        (-0.011820, 0.042940, 0.968881),
    ),
    "tritan": (
        (1.255528, -0.076749, -0.178779),
        (-0.078411, 0.930809, 0.147602),
        (0.004733, 0.691367, 0.303900),
    ),
}


def _strip_comments(text: str) -> str:
    return re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)


def _block(css: str, selector: str) -> dict[str, str]:
    match = re.search(re.escape(selector) + r"\s*\{(.*?)\n\}", css, flags=re.DOTALL)
    assert match is not None, selector
    declarations = re.findall(r"(--[a-z0-9-]+)\s*:\s*([^;]+);", match.group(1))
    return {name: " ".join(value.split()) for name, value in declarations}


def _tokens() -> dict[str, dict[str, str]]:
    css = _strip_comments(SHELL_CSS.read_text(encoding="utf-8"))
    return {name: _block(css, selector) for name, selector in BLOCKS.items()}


def _theme(theme: str) -> dict[str, str]:
    tokens = _tokens()
    merged = dict(tokens["root"])
    if theme == "dark":
        merged.update(tokens["dark"])
    return merged


def _resolve(tokens: dict[str, str], name: str, depth: int = 0) -> str:
    assert depth < 10, name
    value = tokens[name]
    match = re.fullmatch(r"var\((--[a-z0-9-]+)\)", value)
    return _resolve(tokens, match.group(1), depth + 1) if match else value


def _rgb(value: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"#([0-9a-f]{6})", value.lower())
    assert match is not None, value
    raw = match.group(1)
    return (int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16))


def _linear(channel: float) -> float:
    channel /= 255
    return channel / 12.92 if channel <= 0.03928 else ((channel + 0.055) / 1.055) ** 2.4


def _luminance(rgb: tuple[int, int, int]) -> float:
    r, g, b = (_linear(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(first: str, second: str) -> float:
    """WCAG 2.x contrast ratio of two #rrggbb colors."""
    a, b = _luminance(_rgb(first)), _luminance(_rgb(second))
    return (max(a, b) + 0.05) / (min(a, b) + 0.05)


type Vector = tuple[float, float, float]
type Matrix = tuple[tuple[float, float, float], ...]


def _lab(linear: Vector) -> Vector:
    r, g, b = linear
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883

    def f(t: float) -> float:
        return t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116

    return (116 * f(y) - 16, 500 * (f(x) - f(y)), 200 * (f(y) - f(z)))


def _simulate(hex_color: str, matrix: Matrix | None) -> Vector:
    r, g, b = (_linear(c) for c in _rgb(hex_color))
    if matrix is None:
        return _lab((r, g, b))

    def row(weights: tuple[float, float, float]) -> float:
        return max(0.0, min(1.0, weights[0] * r + weights[1] * g + weights[2] * b))

    return _lab((row(matrix[0]), row(matrix[1]), row(matrix[2])))


def _delta_e(a: Vector, b: Vector) -> float:
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b, strict=True)))


def contrast_rows(theme: str) -> list[tuple[str, str, str, str, str, float, float]]:
    """Resolved (role, fg, bg, fg hex, bg hex, ratio, minimum) rows for evidence."""
    tokens = _theme(theme)
    rows = []
    for fg, bg, minimum, role in PAIRS:
        fg_value, bg_value = _resolve(tokens, fg), _resolve(tokens, bg)
        rows.append(
            (
                role,
                fg,
                bg,
                fg_value,
                bg_value,
                round(contrast(fg_value, bg_value), 2),
                minimum,
            )
        )
    for index in range(1, 7):
        for surface in VIZ_SURFACES:
            fg_value = _resolve(tokens, f"--viz-{index}")
            bg_value = _resolve(tokens, surface)
            rows.append(
                (
                    f"data-viz {index}",
                    f"--viz-{index}",
                    surface,
                    fg_value,
                    bg_value,
                    round(contrast(fg_value, bg_value), 2),
                    UI,
                )
            )
    return rows


def test_token_blocks_exist_and_dark_overrides_every_semantic_color() -> None:
    tokens = _tokens()
    light_colors = {
        name
        for name, value in tokens["root"].items()
        if name.startswith(("--color-", "--viz-")) and RAW_COLOR.search(value)
    }
    # Every color that is a raw value in light is re-decided for dark, except
    # the navigation ink, which sits on the navigation band in both themes.
    assert light_colors - DARK_UNCHANGED == light_colors & set(tokens["dark"]), sorted(
        light_colors - DARK_UNCHANGED - set(tokens["dark"])
    )
    assert {f"--viz-{n}" for n in range(1, 7)} <= set(tokens["dark"])
    assert set(tokens["compact"]) == {
        "--density-stack",
        "--density-cell-block",
        "--density-cell-inline",
        "--density-panel-pad",
        "--density-notice-block",
        "--density-gap",
    }
    assert {name for name in tokens["root"] if name.startswith("--density-")} >= set(
        tokens["compact"]
    )
    # Density never shrinks targets: compact leaves --control-height alone.
    assert "--control-height" not in tokens["compact"]
    assert "--density-row-min" not in tokens["compact"]
    assert tokens["root"]["--duration-2"] == "160ms"
    assert tokens["root"]["--duration-3"] == "240ms"


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_every_token_pair_meets_its_contrast_floor(theme: str) -> None:
    failures = [row for row in contrast_rows(theme) if row[5] < row[6]]
    assert failures == []


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_data_viz_hues_stay_distinct_under_color_vision_deficiency(theme: str) -> None:
    tokens = _theme(theme)
    hues = [_resolve(tokens, f"--viz-{n}") for n in range(1, 7)]
    assert len(set(hues)) == len(hues)
    normal = [_simulate(hue, None) for hue in hues]
    assert (
        min(_delta_e(a, b) for a, b in itertools.combinations(normal, 2))
        >= NORMAL_MIN_DELTA_E
    )
    for kind, matrix in CVD.items():
        simulated = [_simulate(hue, matrix) for hue in hues]
        worst = min(_delta_e(a, b) for a, b in itertools.combinations(simulated, 2))
        assert worst >= CVD_MIN_DELTA_E, (theme, kind, round(worst, 1))


def test_raw_values_live_only_in_the_token_blocks() -> None:
    css = _strip_comments(SHELL_CSS.read_text(encoding="utf-8"))
    for selector in BLOCKS.values():
        css = re.sub(
            re.escape(selector) + r"\s*\{.*?\n\}", "", css, count=1, flags=re.DOTALL
        )
    assert RAW_COLOR.findall(css) == []
    components = _strip_comments(COMPONENT_CSS.read_text(encoding="utf-8"))
    assert RAW_COLOR.findall(components) == []


def test_component_styles_keep_the_flat_ledger_rules() -> None:
    components = _strip_comments(COMPONENT_CSS.read_text(encoding="utf-8"))
    for banned in (
        "box-shadow",
        "gradient(",
        "backdrop-filter",
        "filter: blur",
        "text-shadow",
        "@keyframes",
        "animation:",
        "@import",
        "font-face",
    ):
        assert banned not in components, banned
    # Every transition goes through the motion tokens and reduced motion
    # removes the reveal transitions.
    for value in re.findall(r"transition:\s*([^;]+);", components):
        assert value.strip() in {
            "var(--transition-state)",
            "var(--transition-reveal)",
            "none",
        }, value
    assert "@media (prefers-reduced-motion: reduce)" in components
    assert "@media (forced-colors: active)" in components


def test_design_md_documents_exactly_the_implemented_raw_colors() -> None:
    design = (ROOT / "DESIGN.md").read_text(encoding="utf-8")
    frontmatter = design.split("---", 2)[1]
    colors = frontmatter.split("colors:", 1)[1].split("typography:", 1)[0]
    documented = {value.lower() for value in re.findall(r'"(#[0-9a-fA-F]{6})"', colors)}
    tokens = _tokens()
    implemented = {
        value.lower()
        for block in tokens.values()
        for value in block.values()
        if re.fullmatch(r"#[0-9a-fA-F]{6}", value)
    }
    assert documented == implemented, (
        sorted(documented - implemented),
        sorted(implemented - documented),
    )


def test_axe_is_vendored_licensed_pinned_and_never_precached() -> None:
    script = AXE / "axe.min.js"
    assert hashlib.sha256(script.read_bytes()).hexdigest() == AXE_SHA256
    license_bytes = (AXE / "LICENSE").read_bytes()
    assert hashlib.sha256(license_bytes).hexdigest() == AXE_LICENSE_SHA256
    assert license_bytes.startswith(b"Mozilla Public License, version 2.0")
    assert (AXE / "LICENSE-3RD-PARTY.txt").is_file()
    assert not any(asset.startswith("vendor/axe") for asset in SHELL_STATIC_ASSETS)
    worker = (ROOT / "templates" / "clinic-os-sw.js").read_text(encoding="utf-8")
    assert "axe" not in worker
    for template in (ROOT / "templates").rglob("*.html"):
        assert "vendor/axe" not in template.read_text(encoding="utf-8"), template
