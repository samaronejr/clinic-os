"""Lighthouse-like performance budget for the public website (todo 70).

No Node toolchain is allowed outside ``frontend/`` (ADR-001), so this measures
Largest Contentful Paint with Chromium DevTools instead of Lighthouse. It
applies Lighthouse's DevTools throttling for its mobile "Slow 4G" profile:
562.5 ms request latency, 1474.56 Kbps down, 675 Kbps up and 4x CPU. The cache
is disabled, so every page is a cold load. A page passes when its LCP is under
2.5 s, the "good" threshold of Core Web Vitals.

The ``website`` browser suite calls :func:`measure_page` under the renewal
runner (Chromium leg). The CLI measures any running loopback server, given
``--base-url http://127.0.0.1:<port>``, ``--executable <chromium>`` and
``--report <file.json>``.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Final, Never

if TYPE_CHECKING:
    from collections.abc import Sequence

    from playwright.sync_api import Page, ViewportSize

LCP_BUDGET_MS: Final = 2500.0
KBPS_TO_BYTES_PER_SECOND: Final = 1024 / 8
# Lighthouse throttling.md, "mobileSlow4G" DevTools (applied) throttling.
SLOW_4G: Final = {
    "offline": False,
    "latency": 562.5,
    "downloadThroughput": 1474.56 * KBPS_TO_BYTES_PER_SECOND,
    "uploadThroughput": 675 * KBPS_TO_BYTES_PER_SECOND,
}
CPU_SLOWDOWN: Final = 4.0
MOBILE_VIEWPORT: Final[ViewportSize] = {"width": 412, "height": 823}
PAGES: Final = (
    "/",
    "/product/",
    "/security/",
    "/contact/",
    "/brand/",
    "/help/",
    "/help/book-appointment/",
)
LCP_JS: Final = """() => new Promise((resolve) => {
  const navigation = performance.getEntriesByType('navigation')[0];
  new PerformanceObserver((list) => {
    const entries = list.getEntries();
    const last = entries[entries.length - 1];
    resolve({
      lcp_ms: last.startTime,
      lcp_element: last.element ? last.element.tagName.toLowerCase() : null,
      // DevTools throttling does not delay responseStart, so it would read
      // ~3 ms; responseEnd is when the throttled document body arrived.
      document_received_ms: navigation ? navigation.responseEnd : null,
      transfer_bytes: performance.getEntriesByType('resource')
        .reduce((sum, entry) => sum + entry.transferSize, navigation
          ? navigation.transferSize : 0),
    });
  }).observe({type: 'largest-contentful-paint', buffered: true});
  // A deadline, not a wait: buffered entries arrive at once after load, and
  // a page that never reports an LCP fails instead of hanging the run.
  setTimeout(() => resolve({lcp_ms: null}), 10000);
})"""


class PerfBudgetError(RuntimeError):
    """The measurement could not run as specified."""


def _fail(message: str) -> Never:
    raise PerfBudgetError(message)


@dataclass(frozen=True, slots=True)
class Measurement:
    """One cold load of one page under the throttled profile."""

    path: str
    status: int
    lcp_ms: float
    lcp_element: str | None
    document_received_ms: float | None
    transfer_bytes: int

    @property
    def within_budget(self) -> bool:
        """Report whether this load met the LCP budget."""
        return self.status == HTTPStatus.OK and self.lcp_ms < LCP_BUDGET_MS


def throttle(page: Page) -> None:
    """Apply the Slow 4G network, a cold cache and 4x CPU to ``page``."""
    session = page.context.new_cdp_session(page)
    session.send("Network.enable")
    session.send("Network.setCacheDisabled", {"cacheDisabled": True})
    session.send("Network.emulateNetworkConditions", dict(SLOW_4G))
    session.send("Emulation.setCPUThrottlingRate", {"rate": CPU_SLOWDOWN})


def measure_page(page: Page, base_url: str, path: str) -> Measurement:
    """Cold-load ``path`` on an already throttled page and read its LCP."""
    response = page.goto(base_url + path, wait_until="load")
    if response is None:
        _fail(f"no response for {path}")
    values = page.evaluate(LCP_JS)
    if values.get("lcp_ms") is None:
        _fail(f"{path} reported no largest contentful paint")
    return Measurement(
        path=path,
        status=response.status,
        lcp_ms=float(values["lcp_ms"]),
        lcp_element=values["lcp_element"],
        document_received_ms=values["document_received_ms"],
        transfer_bytes=int(values["transfer_bytes"]),
    )


def verdict(measurements: Sequence[Measurement]) -> dict[str, object]:
    """Summarize measurements; the run passes only if every page passes."""
    if not measurements:
        _fail("no page was measured")
    return {
        "budget": {"lcp_ms": LCP_BUDGET_MS},
        "profile": {
            "network": "Lighthouse mobile Slow 4G (DevTools throttling)",
            "latency_ms": SLOW_4G["latency"],
            "download_kbps": 1474.56,
            "upload_kbps": 675,
            "cpu_slowdown": CPU_SLOWDOWN,
            "viewport": MOBILE_VIEWPORT,
            "cache": "disabled",
        },
        "pages": [
            {**asdict(item), "within_budget": item.within_budget}
            for item in measurements
        ],
        "passed": all(item.within_budget for item in measurements),
    }


def run(base_url: str, executable: str) -> dict[str, object]:
    """Measure every website page in a fresh Chromium context each."""
    from playwright.sync_api import sync_playwright  # noqa: PLC0415 - optional CLI

    measurements = []
    with sync_playwright() as driver:
        browser = driver.chromium.launch(executable_path=executable)
        try:
            for path in PAGES:
                context = browser.new_context(
                    viewport=MOBILE_VIEWPORT, is_mobile=True, has_touch=True
                )
                try:
                    page = context.new_page()
                    throttle(page)
                    measurements.append(measure_page(page, base_url, path))
                finally:
                    context.close()
        finally:
            browser.close()
    return verdict(measurements)


def main(argv: Sequence[str] | None = None) -> int:
    """Measure, write the JSON report and exit non-zero over budget."""
    parser = argparse.ArgumentParser(prog="python -m ops.testing.website_perf")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--executable", required=True)
    parser.add_argument("--report", required=True, type=Path)
    arguments = parser.parse_args(argv)
    if not arguments.base_url.startswith(("http://127.0.0.1:", "http://localhost:")):
        _fail("--base-url must be a loopback http origin")
    report = run(arguments.base_url.rstrip("/"), arguments.executable)
    arguments.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    sys.stdout.write(f"website LCP budget passed={report['passed']}\n")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
