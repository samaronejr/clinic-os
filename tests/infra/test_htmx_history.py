"""No page keeps htmx history HTML in browser storage (SD-8b).

Before every boosted or pushed navigation, htmx 2 copies the whole ``<body>``
into ``sessionStorage["htmx-history-cache"]`` unless ``historyCacheSize`` is 0,
and it applies only the first ``htmx-config`` meta of a document. The shell is
the only template that loads htmx, so its configuration must be the first one
of every rendered page; the browser suite ``patient-workspace`` proves the
running effect.
"""

from __future__ import annotations

import json
from html.parser import HTMLParser
from pathlib import Path
from uuid import uuid4

from apps.core.workspace import DENIED_ATTRIBUTE
from django.template import Engine, engines
from django.template.loader import render_to_string
from django.template.loader_tags import BlockNode
from django.test import Client, RequestFactory

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = PROJECT_ROOT / "templates"
HTMX_SCRIPT = "vendor/htmx/htmx.min.js"
# Shell pages through each inheritance route: the bare shell, the refusal pages
# (workspace_refusal hook), the patient workspace and the missing-session gate.
SHELL_PAGES = (
    "base.html",
    "403.html",
    "404.html",
    "ehr/patient_workspace.html",
    "intake/patient_gate.html",
)
# A competing config that also closes an enclosing attribute, so it renders as
# an element wherever a child block lands.
COMPETING = (
    '"><meta name="htmx-config" content=\''
    '{"historyCacheSize":7,"refreshOnHistoryMiss":false}\'>'
)


class _HtmxConfigs(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.contents: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "meta" and values.get("name") == "htmx-config":
            self.contents.append(values.get("content") or "")


def htmx_configs(markup: str) -> list[dict[str, object]]:
    """Every htmx config a document declares, in document order."""
    scan = _HtmxConfigs()
    scan.feed(markup)
    return [json.loads(content) for content in scan.contents]


def htmx_config(markup: str) -> dict[str, object]:
    """The configuration htmx applies: the document's first htmx-config meta."""
    configs = htmx_configs(markup)
    assert configs
    return configs[0]


def _keeps_no_history_html(config: dict[str, object]) -> bool:
    # With no cache every Back misses; a reload asks the server for the whole
    # page instead of swapping an HX-Request fragment into <body>.
    return (
        config.get("historyCacheSize") == 0
        and config.get("refreshOnHistoryMiss") is True
    )


def test_only_the_shell_loads_htmx() -> None:
    loaders = sorted(
        str(path.relative_to(TEMPLATES))
        for path in TEMPLATES.rglob("*")
        if path.is_file() and HTMX_SCRIPT in path.read_text(encoding="utf-8")
    )

    assert loaders == ["base.html"]


def test_rendered_pages_carry_only_the_privacy_config() -> None:
    pages: dict[str, str] = {
        name: render_to_string(name, {"clinic_id": uuid4()}) for name in SHELL_PAGES
    }
    served = Client().get("/auth/login/")
    assert served.status_code == 200
    pages["/auth/login/"] = served.content.decode()

    configs = {page: htmx_configs(markup) for page, markup in pages.items()}

    assert {page: len(found) for page, found in configs.items()} == dict.fromkeys(
        pages, 1
    )
    assert all(_keeps_no_history_html(found[0]) for found in configs.values())


def test_no_child_block_can_pre_empt_the_privacy_config() -> None:
    shell = Engine.get_default().get_template("base.html")
    blocks = sorted(
        node.name
        for node in shell.nodelist.get_nodes_by_type(BlockNode)
        if isinstance(node, BlockNode)
    )
    assert {"workspace_refusal", "meta_description", "extra_head"} <= set(blocks)
    child = engines["django"].from_string(
        '{% extends "base.html" %}'
        + "".join(f"{{% block {name} %}}{COMPETING}{{% endblock %}}" for name in blocks)
    )

    markup = child.render({})
    configs = htmx_configs(markup)

    # Competing configs did render, yet the shell's comes first and nothing
    # precedes the doctype.
    assert len(configs) > 1
    assert _keeps_no_history_html(configs[0])
    assert markup.lstrip().startswith("<!doctype html>")


def test_refusal_hook_keeps_its_effect_when_its_output_is_dropped() -> None:
    request = RequestFactory().get("/")

    render_to_string("403.html", request=request)

    assert getattr(request, DENIED_ATTRIBUTE) is True
