from __future__ import annotations

import re
from pathlib import Path

from django.contrib.staticfiles import finders


def _asset_text(path: str) -> str:
    located = finders.find(path)
    assert isinstance(located, str)
    return Path(located).read_text(encoding="utf-8")


def _pure_lines(stylesheet: str) -> int:
    return sum(bool(line.strip()) for line in stylesheet.splitlines())


def test_linked_stylesheets_are_split_into_reviewable_modules() -> None:
    for path in ("css/clinic-os.css", "css/clinic-os-auth.css"):
        assert _pure_lines(_asset_text(path)) <= 250


def test_design_tokens_control_typography_and_tablet_gutters() -> None:
    styles = re.sub(r"\s+", " ", _asset_text("css/clinic-os-auth.css"))
    shell_styles = re.sub(r"\s+", " ", _asset_text("css/clinic-os.css"))

    assert "font-family: var(--font-display)" in styles
    assert "h2 {" in shell_styles
    assert "font-weight: 650" in shell_styles
    assert "@media (min-width: 48rem)" in shell_styles
    assert "padding-inline: var(--space-5)" in shell_styles


def test_styles_include_shrink_wrapping_and_locale_aware_breaking() -> None:
    styles = _asset_text("css/clinic-os-auth.css")
    shell_styles = _asset_text("css/clinic-os.css")

    assert "min-width: 0" in styles
    assert "overflow-wrap: anywhere" in styles
    assert ":lang(ko)" in shell_styles
    assert "word-break: keep-all" in shell_styles
    assert ":lang(ja)" in shell_styles
    assert ":lang(zh)" in shell_styles
    assert ".keep-phrase" in shell_styles
    assert "white-space: nowrap" in shell_styles
    assert "transition: none" in styles


def test_showcase_groups_localized_semantic_phrases() -> None:
    showcase = (
        Path(__file__).resolve().parents[1] / "templates" / "identity" / "showcase.html"
    ).read_text(encoding="utf-8")

    for phrase in ("다음 단계로", "確認して", "安全地"):
        assert f'<span class="keep-phrase">{phrase}</span>' in showcase


def test_auth_script_manages_busy_state_and_error_focus() -> None:
    script = _asset_text("js/auth-ui.js")

    for event_name in (
        "htmx:beforeRequest",
        "htmx:afterRequest",
        "htmx:responseError",
        "htmx:afterSwap",
    ):
        assert event_name in script
    assert "aria-busy" in script
    assert 'setAttribute("aria-busy", "true")' in script
    assert 'removeAttribute("aria-busy")' in script
    assert ".disabled" in script
    assert "data-focus-error" in script


def test_every_htmx_auth_form_uses_the_shared_lifecycle() -> None:
    template_root = Path(__file__).resolve().parents[1] / "templates" / "identity"
    expected_forms = {
        "login.html": 1,
        "enroll.html": 2,
        "verify.html": 1,
        "logout.html": 1,
    }

    for template_name, expected_count in expected_forms.items():
        source = (template_root / template_name).read_text(encoding="utf-8")
        assert source.count("data-auth-form") == expected_count


def test_each_auth_route_defines_a_meta_description() -> None:
    template_root = Path(__file__).resolve().parents[1] / "templates" / "identity"
    for template_name in (
        "login.html",
        "enroll.html",
        "verify.html",
        "logout.html",
        "protected.html",
        "showcase.html",
    ):
        source = (template_root / template_name).read_text(encoding="utf-8")
        assert "{% block meta_description %}" in source
