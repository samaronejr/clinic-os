"""CSP-safe replacement for ``Page.wait_for_function``, equally strict.

Playwright's ``wait_for_function`` re-evaluates its predicate with
``globalThis.eval`` in the page's main world, which the strict
``script-src 'self'`` policy refuses as ``'unsafe-eval'``. ``wait_for_js``
compiles the predicate as part of one ``page.evaluate`` call (DevTools
compiles it, outside the page's CSP) and re-checks it on every animation
frame, like Playwright's default ``raf`` polling. The page reports the outcome
as a console message carrying a per-call token.

The deadline is Playwright's own: the outcome is awaited with
``page.expect_console_message``, subscribed before the watcher starts, so an
explicit ``timeout`` or the page's ``set_default_timeout`` applies exactly as it
does to ``wait_for_function`` (``0`` disables it). Expiry raises Playwright's
``TimeoutError`` independently of animation-frame progress, and a predicate
that turns truthy after the deadline can never turn the failure into success:
the watcher is told to stop. A predicate exception raises ``Error``. A
navigation destroys the watcher, so the wait then fails at its deadline
instead of silently passing.
"""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING, Final

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

if TYPE_CHECKING:
    from playwright.sync_api import ConsoleMessage, JSHandle, Page

_ABORTED: Final = "__clinicOsAbortedWaits"
_WATCH_HEAD: Final = """([token, arg]) => {
  const candidate = () => ("""
_WATCH_TAIL: Final = f""");
  const tick = () => {{
    if (window.{_ABORTED} && window.{_ABORTED}.has(token)) {{
      return;
    }}
    let value;
    try {{
      value = candidate();
      if (typeof value === "function") {{
        value = value(arg);
      }}
    }} catch (error) {{
      console.debug(token + ":error", String(error && error.message || error));
      return;
    }}
    if (value) {{
      console.debug(token + ":ok", value);
      return;
    }}
    requestAnimationFrame(tick);
  }};
  tick();
}}"""
_ABORT: Final = f"""(token) => {{
  window.{_ABORTED} = window.{_ABORTED} || new Set();
  window.{_ABORTED}.add(token);
}}"""


class WaitPredicateError(PlaywrightError):
    """The awaited predicate threw inside the page."""

    def __init__(self, detail: str) -> None:
        super().__init__(f"wait_for_js predicate threw: {detail}")


def wait_for_js(
    page: Page,
    expression: str,
    *,
    arg: object = None,
    timeout: float | None = None,
) -> JSHandle:
    """Return the truthy value of ``expression`` (or of the function it denotes).

    ``timeout`` is in milliseconds; ``None`` uses the page's default timeout.
    """
    token = f"clinic-os-wait-{secrets.token_hex(8)}"
    ok, failed = f"{token}:ok", f"{token}:error"

    def settled(message: ConsoleMessage) -> bool:
        return message.type == "debug" and message.text.startswith((ok, failed))

    source = f"{_WATCH_HEAD}{expression}{_WATCH_TAIL}"
    try:
        with page.expect_console_message(settled, timeout=timeout) as outcome:
            page.evaluate(source, [token, arg])
    except PlaywrightTimeoutError:
        # Stop the watcher so a late truthy value is never reported; if the
        # page is gone this raises too, chained to the timeout.
        page.evaluate(_ABORT, token)
        raise
    message = outcome.value
    if message.text.startswith(failed):
        raise WaitPredicateError(message.text.removeprefix(failed).strip())
    return message.args[1]
