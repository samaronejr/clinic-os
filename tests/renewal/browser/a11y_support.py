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

from renewal.browser._page_wait import _answer, evaluate_js

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import Page

AXE_URL: Final = "/static/vendor/axe/axe.min.js"
BLOCKING_IMPACTS: Final = frozenset({"serious", "critical"})
OK_STATUS: Final = 200
AXE_OPTIONS_JS: Final = """{
    runOnly: {type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa',
      'wcag22aa', 'best-practice']},
    resultTypes: ['violations'],
  }"""
AXE_SUMMARY_JS: Final = """(result) => result.violations.map((v) => ({
    id: v.id, impact: v.impact, help: v.help,
    nodes: v.nodes.slice(0, 5).map((n) => n.target.join(' ')),
    count: v.nodes.length,
  }))"""
AXE_RUN_JS: Final = (
    "async () => ("
    + AXE_SUMMARY_JS
    + ")(await axe.run(document, "
    + AXE_OPTIONS_JS
    + "))"
)
# A paused Playwright clock never fires axe-core's per-rule setTimeout(0):
# start the run without awaiting it, then step the clock until it settles.
AXE_START_JS: Final = (
    "() => { window.__clinicAxe = null; axe.run(document, "
    + AXE_OPTIONS_JS
    + ").then((result) => { window.__clinicAxe = {violations: ("
    + AXE_SUMMARY_JS
    + ")(result)}; }, (error) => { window.__clinicAxe = {error: String(error)}; }); }"
)
AXE_SETTLED_JS: Final = "() => window.__clinicAxe"
AXE_LOADED_JS: Final = (
    "() => typeof window.axe === 'object' && typeof axe.run === 'function'"
)
PAUSED_CLOCK_STEPS: Final = 200


def blocking(violations: list[dict[str, object]]) -> list[dict[str, object]]:
    """Return the violations whose impact fails a suite."""
    return [v for v in violations if v.get("impact") in BLOCKING_IMPACTS]


def _settle_under_paused_clock(page: Page) -> list[dict[str, object]]:
    """Run axe with only zero-delay timers firing, one paused-clock ms per step."""
    page.evaluate(AXE_START_JS)
    for _ in range(PAUSED_CLOCK_STEPS):
        page.clock.run_for(1)
        settled = page.evaluate(AXE_SETTLED_JS)
        if settled is not None:
            assert "error" not in settled, settled["error"]
            violations: list[dict[str, object]] = settled["violations"]
            return violations
    msg = f"axe did not settle within {PAUSED_CLOCK_STEPS} paused-clock milliseconds"
    raise AssertionError(msg)


def check_page(
    page: Page,
    base_url: str,
    artifact_root: Path,
    state: str,
    *,
    paused_clock: bool = False,
) -> dict[str, object]:
    """Run axe on the page as it is now; fail on serious/critical violations.

    The vendored axe-core is injected once per document, and that injection's
    response must come from the supervised server; a later check on the same
    document reuses it (a second injection is served without a response).
    ``paused_clock`` is for a page whose Playwright clock is paused: axe then
    settles one clock millisecond at a time, so no later page timer can fire.
    """
    if not evaluate_js(page, AXE_LOADED_JS):
        with page.expect_response(f"{base_url}{AXE_URL}") as axe_response:
            _answer(
                page,
                page._impl_obj.add_script_tag(url=f"{base_url}{AXE_URL}"),
                None,
            )
        assert axe_response.value.status == OK_STATUS
    violations: list[dict[str, object]] = (
        _settle_under_paused_clock(page)
        if paused_clock
        else evaluate_js(page, AXE_RUN_JS)
    )
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
