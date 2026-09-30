"""Strict Content-Security-Policy header, extension registry, and markup scan."""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest
from apps.core import middleware
from apps.core.middleware import (
    CSP_HEADER,
    CSP_REPORT_ONLY_HEADER,
    CspExtension,
    CspExtensionError,
    content_security_policy,
    register_csp_extension,
)
from config.settings.contracts import resolve_csp_report_only
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.test import Client, override_settings

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = PROJECT_ROOT / "templates"
STATIC_JS = PROJECT_ROOT / "static" / "js"
# The exact policy of todo 10; any drift is a security review, not a refactor.
EXPECTED_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; "
    "worker-src 'self'; object-src 'none'; base-uri 'none'; "
    "frame-ancestors 'none'; form-action 'self'"
)
CSP_MIDDLEWARE = "apps.core.middleware.ContentSecurityPolicyMiddleware"
WHITENOISE_MIDDLEWARE = "whitenoise.middleware.WhiteNoiseMiddleware"
PROVIDER_ORIGIN = "https://video.sintetico.invalid"


@pytest.fixture
def empty_registry(monkeypatch: pytest.MonkeyPatch) -> list[CspExtension]:
    registry: list[CspExtension] = []
    monkeypatch.setattr(middleware, "_CSP_EXTENSIONS", registry)
    return registry


def test_base_policy_is_exactly_the_strict_first_party_policy() -> None:
    policy = content_security_policy("/")

    assert policy == EXPECTED_POLICY
    assert "unsafe-inline" not in policy
    assert "unsafe-eval" not in policy


def test_csp_middleware_sits_directly_below_whitenoise() -> None:
    order = list(settings.MIDDLEWARE)

    assert order.index(CSP_MIDDLEWARE) == order.index(WHITENOISE_MIDDLEWARE) + 1


def test_public_page_is_served_with_the_enforced_policy() -> None:
    response = Client().get("/")

    assert response.status_code == 200
    assert response.headers[CSP_HEADER] == EXPECTED_POLICY
    assert CSP_REPORT_ONLY_HEADER not in response.headers


@pytest.mark.django_db(transaction=True)
def test_tenant_refusal_carries_the_enforced_policy() -> None:
    response = Client().get("/auth/protected/")

    assert response.status_code == 403
    assert response.headers[CSP_HEADER] == EXPECTED_POLICY


@override_settings(CLINIC_CSP_REPORT_ONLY=True)
def test_report_only_flag_switches_only_the_header_name() -> None:
    response = Client().get("/")

    assert response.headers[CSP_REPORT_ONLY_HEADER] == EXPECTED_POLICY
    assert CSP_HEADER not in response.headers


@pytest.mark.parametrize(
    ("environment", "data_mode", "expected"),
    [
        ({}, "synthetic", False),
        ({"CLINIC_CSP_REPORT_ONLY": "true"}, "synthetic", True),
        ({"CLINIC_CSP_REPORT_ONLY": "false"}, "synthetic", False),
        ({"CLINIC_CSP_REPORT_ONLY": "sim"}, "synthetic", False),
        ({}, "live", False),
        ({"CLINIC_CSP_REPORT_ONLY": "false"}, "live", False),
    ],
)
def test_report_only_resolution_defaults_to_enforcement(
    environment: dict[str, str],
    data_mode: str,
    expected: bool,
) -> None:
    assert resolve_csp_report_only(environment, data_mode) is expected


def test_live_mode_refuses_report_only() -> None:
    with pytest.raises(ImproperlyConfigured, match="only in synthetic data mode"):
        resolve_csp_report_only({"CLINIC_CSP_REPORT_ONLY": "true"}, "live")


def test_active_extension_widens_only_its_directive_on_its_route(
    empty_registry: list[CspExtension],
) -> None:
    register_csp_extension(
        CspExtension(
            path_prefix="/teleconsult/",
            directive="connect-src",
            origins=(PROVIDER_ORIGIN, PROVIDER_ORIGIN),
            is_active=lambda: True,
        )
    )

    widened = content_security_policy("/teleconsult/sessions/")
    assert widened == EXPECTED_POLICY.replace(
        "connect-src 'self'", f"connect-src 'self' {PROVIDER_ORIGIN}"
    )
    assert content_security_policy("/scheduling/") == EXPECTED_POLICY


def test_inactive_extension_leaves_the_base_policy(
    empty_registry: list[CspExtension],
) -> None:
    register_csp_extension(
        CspExtension(
            path_prefix="/teleconsult/",
            directive="media-src",
            origins=(PROVIDER_ORIGIN,),
            is_active=lambda: False,
        )
    )

    assert content_security_policy("/teleconsult/sessions/") == EXPECTED_POLICY


def test_no_extension_is_registered_until_a_provider_is_activated() -> None:
    # Provider activation (plan item 4) registers origins; none exist yet.
    assert middleware._CSP_EXTENSIONS == []


@pytest.mark.parametrize(
    ("path_prefix", "directive", "origins"),
    [
        ("/teleconsult/", "script-src", (PROVIDER_ORIGIN,)),
        ("/teleconsult/", "style-src", (PROVIDER_ORIGIN,)),
        ("/teleconsult/", "frame-ancestors", (PROVIDER_ORIGIN,)),
        ("/teleconsult/", "connect-src", ("'unsafe-inline'",)),
        ("/teleconsult/", "connect-src", ("*",)),
        ("/teleconsult/", "connect-src", ("https://*.sintetico.invalid",)),
        ("/teleconsult/", "connect-src", ("http://video.sintetico.invalid",)),
        ("/teleconsult/", "connect-src", ("https://video.sintetico.invalid/x",)),
        ("/teleconsult/", "connect-src", ("https://localhost",)),
        ("/teleconsult/", "connect-src", ()),
        ("teleconsult/", "connect-src", (PROVIDER_ORIGIN,)),
        ("/", "connect-src", (PROVIDER_ORIGIN,)),
    ],
)
def test_registry_refuses_extensions_that_weaken_the_policy(
    empty_registry: list[CspExtension],
    path_prefix: str,
    directive: str,
    origins: tuple[str, ...],
) -> None:
    extension = CspExtension(
        path_prefix=path_prefix,
        directive=directive,
        origins=origins,
        is_active=lambda: True,
    )

    with pytest.raises(CspExtensionError):
        register_csp_extension(extension)
    assert empty_registry == []


class _MarkupScan(HTMLParser):
    """Collect markup the strict CSP would refuse to execute or apply."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.findings: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        names = {name for name, _ in attrs}
        if tag == "script" and "src" not in names:
            self.findings.append("inline <script>")
        if tag == "style":
            self.findings.append("inline <style>")
        for name, value in attrs:
            if name == "style":
                self.findings.append("style attribute")
            if name.startswith("on"):
                self.findings.append(f"{name} handler")
            if name.startswith(("hx-on", "hx-vars", "data-hx-on")):
                self.findings.append(f"{name} (eval)")
            if value and value.strip().lower().startswith(("javascript:", "js:")):
                self.findings.append(f"{name} script URL")


# Template comments are dropped; tags and variables become one inert token
# so nested quotes such as aria-label="{% trans "x" %}" keep attributes whole.
TEMPLATE_COMMENT = re.compile(
    r"\{% comment %\}.*?\{% endcomment %\}|\{#.*?#\}", re.DOTALL
)
TEMPLATE_SYNTAX = re.compile(r"\{%.*?%\}|\{\{.*?\}\}", re.DOTALL)


def csp_markup_findings(source: str) -> list[str]:
    """Return every CSP-blocked construct in one template source."""
    markup = TEMPLATE_SYNTAX.sub("T", TEMPLATE_COMMENT.sub("", source))
    scan = _MarkupScan()
    scan.feed(markup)
    scan.close()
    return scan.findings


def test_scanner_detects_inline_script_and_style() -> None:
    source = (
        '<script>alert(1)</script><div style="x" onclick="y" hx-on:click="z">'
        '<a href="javascript:void(0)"></a><style>p{}</style>'
        "<script src=\"{% static 'js/auth-ui.js' %}\"></script>"
        '<nav aria-label="{% trans "Shown on this page" %}"></nav>'
        "{% comment %}<script>ignored()</script>{% endcomment %}"
    )

    assert csp_markup_findings(source) == [
        "inline <script>",
        "style attribute",
        "onclick handler",
        "hx-on:click (eval)",
        "href script URL",
        "inline <style>",
    ]


def test_templates_contain_nothing_the_strict_policy_blocks() -> None:
    findings = {
        str(path.relative_to(PROJECT_ROOT)): found
        for path in sorted(TEMPLATES.rglob("*.html"))
        if (found := csp_markup_findings(path.read_text(encoding="utf-8")))
    }

    assert findings == {}


def test_static_scripts_never_evaluate_strings_or_set_inline_styles() -> None:
    forbidden = re.compile(
        r"\beval\s*\(|new\s+Function\s*\(|setTimeout\s*\(\s*['\"`]"
        r"|setInterval\s*\(\s*['\"`]|setAttribute\s*\(\s*['\"]style['\"]"
        r"|\.cssText\b|insertRule\s*\("
    )
    findings = {
        path.name: forbidden.findall(path.read_text(encoding="utf-8"))
        for path in sorted(STATIC_JS.glob("*.js"))
    }

    assert {name: hits for name, hits in findings.items() if hits} == {}


class _MetaConfig(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.content: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "meta" and values.get("name") == "htmx-config":
            self.content = values.get("content")


def test_rendered_shell_configures_htmx_for_the_strict_policy() -> None:
    response = Client().get("/")
    scan = _MetaConfig()
    scan.feed(response.content.decode())

    assert scan.content is not None
    config = json.loads(scan.content)
    assert config["allowEval"] is False
    assert config["includeIndicatorStyles"] is False
    # Settling "style" would copy a live inline style attribute onto swapped
    # markup, which style-src 'self' refuses.
    assert config["attributesToSettle"] == ["class", "width", "height"]
