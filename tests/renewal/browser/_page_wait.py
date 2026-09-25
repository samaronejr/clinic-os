"""CSP-safe replacement for ``Page.wait_for_function``.

Playwright's ``wait_for_function`` re-evaluates its predicate with
``globalThis.eval`` in the page's main world, which the strict
``script-src 'self'`` policy refuses as ``'unsafe-eval'``. ``wait_for_js``
instead compiles the predicate as part of one ``evaluate_handle`` call
(compiled by DevTools, outside the page's CSP) and re-checks it on every
animation frame inside the page, like Playwright's default ``raf`` polling.
A navigation that replaces the execution context restarts the wait, as
Playwright does; any other error propagates.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Final

from playwright.sync_api import Error as PlaywrightError

if TYPE_CHECKING:
    from playwright.sync_api import JSHandle, Page

DEFAULT_TIMEOUT_MS: Final = 30_000
_CONTEXT_REPLACED: Final = "Execution context was destroyed"
_WAIT_HEAD: Final = "async ({ arg, timeout }) => {\n  const candidate = () => ("
_WAIT_TAIL: Final = """);
  const deadline = performance.now() + timeout;
  for (;;) {
    let value = candidate();
    if (typeof value === "function") {
      value = value(arg);
    }
    if (value) {
      return value;
    }
    if (performance.now() > deadline) {
      throw new Error("wait_for_js: predicate stayed falsy until the deadline");
    }
    await new Promise((resolve) => requestAnimationFrame(resolve));
  }
}"""


def wait_for_js(
    page: Page,
    expression: str,
    *,
    arg: object = None,
    timeout: float = DEFAULT_TIMEOUT_MS,
) -> JSHandle:
    """Resolve once ``expression`` (or the function it denotes) is truthy."""
    source = f"{_WAIT_HEAD}{expression}{_WAIT_TAIL}"
    deadline = time.monotonic() + timeout / 1000
    while True:
        remaining_ms = max((deadline - time.monotonic()) * 1000, 0)
        try:
            return page.evaluate_handle(source, {"arg": arg, "timeout": remaining_ms})
        except PlaywrightError as error:
            if _CONTEXT_REPLACED not in error.message or remaining_ms == 0:
                raise
