"""Shared axe-core check for runner browser suites.

Injects the vendored axe-core (``static/vendor/axe/axe.min.js``, sha256
pinned in ``tests/renewal/test_design_tokens.py``) from the supervised
server into the page under test, runs the WCAG 2.x A/AA + best-practice
rules against the current page state, writes every violation to
``<artifact_root>/axe/<state>.json`` and fails on any ``serious`` or
``critical`` one. Lower-impact violations stay in the evidence file.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import Page

AXE_URL: Final = "/static/vendor/axe/axe.min.js"
BLOCKING_IMPACTS: Final = frozenset({"serious", "critical"})
OK_STATUS: Final = 200
AXE_RUN_JS: Final = """async () => {
  const result = await axe.run(document, {
    runOnly: {type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa',
      'wcag22aa', 'best-practice']},
    resultTypes: ['violations'],
  });
  return result.violations.map((v) => ({
    id: v.id, impact: v.impact, help: v.help,
    nodes: v.nodes.slice(0, 5).map((n) => n.target.join(' ')),
    count: v.nodes.length,
  }));
}"""


def blocking(violations: list[dict[str, object]]) -> list[dict[str, object]]:
    """Return the violations whose impact fails a suite."""
    return [v for v in violations if v.get("impact") in BLOCKING_IMPACTS]


def check_page(
    page: Page, base_url: str, artifact_root: Path, state: str
) -> dict[str, object]:
    """Run axe on the page as it is now; fail on serious/critical violations."""
    with page.expect_response(f"{base_url}{AXE_URL}") as axe_response:
        page.add_script_tag(url=f"{base_url}{AXE_URL}")
    assert axe_response.value.status == OK_STATUS
    violations: list[dict[str, object]] = page.evaluate(AXE_RUN_JS)
    destination = artifact_root / "axe" / f"{state}.json"
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination.write_text(json.dumps(violations, sort_keys=True, indent=2) + "\n")
    destination.chmod(0o600)
    failing = blocking(violations)
    assert failing == [], (
        state,
        [(v["id"], v["impact"], v["nodes"]) for v in failing],
    )
    return {
        "axe_report": f"axe/{destination.name}",
        "state": state,
        "violations": len(violations),
    }
