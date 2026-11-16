"""No page keeps htmx history HTML in browser storage (SD-8b).

Before every boosted or pushed navigation, htmx 2 copies the whole ``<body>``
into ``sessionStorage["htmx-history-cache"]`` unless ``historyCacheSize`` is 0.
The shell is the only template that loads htmx, so its configuration governs
every page; the browser suite ``patient-workspace`` proves the running effect.
"""

from __future__ import annotations

import json
from html.parser import HTMLParser
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = PROJECT_ROOT / "templates"
SHELL = TEMPLATES / "base.html"
HTMX_SCRIPT = "vendor/htmx/htmx.min.js"
CONFIG_META = 'name="htmx-config"'


class _HtmxConfig(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.content: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        first = self.content is None
        # htmx reads the first htmx-config meta of the document.
        if first and tag == "meta" and values.get("name") == "htmx-config":
            self.content = values.get("content")


def htmx_config(markup: str) -> dict[str, object]:
    """The htmx configuration a document (or the shell template) declares."""
    scan = _HtmxConfig()
    scan.feed(markup)
    assert scan.content is not None
    config: dict[str, object] = json.loads(scan.content)
    return config


def test_only_the_shell_loads_htmx() -> None:
    loaders = sorted(
        str(path.relative_to(TEMPLATES))
        for path in TEMPLATES.rglob("*")
        if path.is_file() and HTMX_SCRIPT in path.read_text(encoding="utf-8")
    )

    assert loaders == ["base.html"]


def test_shell_keeps_no_history_html_and_reloads_on_back() -> None:
    shell = SHELL.read_text(encoding="utf-8")
    # Outside every block, so no child template can replace or drop it.
    head = shell[: shell.index(CONFIG_META)]
    assert head.count("{% block") == head.count("{% endblock")

    config = htmx_config(shell)
    assert config["historyCacheSize"] == 0
    # With no cache every Back misses; a reload asks the server for the whole
    # page instead of swapping an HX-Request fragment into <body>.
    assert config["refreshOnHistoryMiss"] is True
