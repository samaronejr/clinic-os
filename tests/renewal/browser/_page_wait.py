"""CSP-safe replacement for ``Page.wait_for_function``, equally strict.

Playwright's ``wait_for_function`` re-evaluates its predicate with
``globalThis.eval`` in the page's main world, which the strict
``script-src 'self'`` policy refuses as ``'unsafe-eval'``. ``wait_for_js``
compiles the predicate as part of one ``page.evaluate`` call (DevTools
compiles it, outside the page's CSP). It checks the predicate once
immediately, which also works in ``java_script_enabled=False`` contexts, and
then on animation frames, like Playwright's default ``raf`` polling. The page
reports the outcome as a console message that carries a per-call token.

The deadline is enforced in Playwright's driver event loop, the same way
``wait_for_function`` races its evaluation against its progress timeout.
The starting evaluation, the console outcome and a timer run concurrently:

- An explicit ``timeout`` or the page's ``set_default_timeout`` applies
  exactly as for ``wait_for_function`` (``0`` disables it).
- ``TimeoutError`` is raised at the deadline even while a predicate keeps the
  renderer busy or never returns, and even with frozen animation frames.
  Nothing on the timeout path waits for the page.
- After the deadline the listener is gone, so a later outcome is never seen.
  The watcher carries the same deadline and stays silent after it, so a late
  truth is never even reported to the console.
- A failed start (for example a syntax error), a closed or crashed page, or a
  predicate exception (``WaitPredicateError``) raises at once. A navigation
  destroys the watcher, so the wait then fails at its deadline.

It uses Playwright 1.61 internals (``_impl_obj``, ``_sync``, the impl-to-API
``mapping``) because the public sync API cannot start an evaluation without
blocking on it.
"""

from __future__ import annotations

import asyncio
import secrets
from typing import TYPE_CHECKING, Any, Final, cast

from playwright._impl._sync_base import mapping
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from playwright._impl._console_message import ConsoleMessage as ConsoleMessageImpl
    from playwright._impl._page import Page as PageImpl
    from playwright.sync_api import JSHandle, Page

_WATCH_HEAD: Final = """([token, arg, timeout]) => {
  const deadline = timeout > 0 ? performance.now() + timeout : Infinity;
  const candidate = () => ("""
_WATCH_TAIL: Final = """);
  const tick = () => {
    if (performance.now() > deadline) {
      return;
    }
    let value;
    try {
      value = candidate();
      if (typeof value === "function") {
        value = value(arg);
      }
    } catch (error) {
      if (performance.now() <= deadline) {
        console.debug(token + ":error", String(error && error.message || error));
      }
      return;
    }
    if (!value) {
      requestAnimationFrame(tick);
    } else if (performance.now() <= deadline) {
      console.debug(token + ":ok", value);
    }
  };
  tick();
}"""


class WaitPredicateError(PlaywrightError):
    """The awaited predicate threw inside the page."""

    def __init__(self, detail: str) -> None:
        super().__init__(f"wait_for_js predicate threw: {detail}")


class WaitTimeoutError(PlaywrightTimeoutError):
    """The predicate stayed falsy until the deadline."""

    def __init__(self, timeout: float) -> None:
        super().__init__(f"wait_for_js: Timeout {timeout:g}ms exceeded.")


class WaitPageGoneError(PlaywrightError):
    """The page closed or crashed while the wait was pending."""

    def __init__(self, event: str) -> None:
        super().__init__(f"wait_for_js: page {event} while waiting")


def _retrieve(task: asyncio.Task[Any]) -> None:
    # The wait already ended (outcome or timeout); a start evaluation that
    # finishes afterwards cannot change it, so its late result is consumed
    # here only to keep asyncio from reporting it as never retrieved.
    if not task.cancelled():
        task.exception()


class _Outcome:
    """The first tokened console outcome, or the page closing or crashing."""

    def __init__(self, impl_page: PageImpl, token: str) -> None:
        self.page = impl_page
        self.prefix = f"{token}:"
        self.future: asyncio.Future[ConsoleMessageImpl] = (
            asyncio.get_running_loop().create_future()
        )
        impl_page.on("console", self.on_console)
        impl_page.on("close", self.on_close)
        impl_page.on("crash", self.on_crash)

    def on_console(self, message: ConsoleMessageImpl) -> None:
        if (
            not self.future.done()
            and message.type == "debug"
            and message.text.startswith(self.prefix)
        ):
            self.future.set_result(message)

    def on_close(self, _page: object) -> None:
        if not self.future.done():
            self.future.set_exception(WaitPageGoneError("closed"))

    def on_crash(self, _page: object) -> None:
        if not self.future.done():
            self.future.set_exception(WaitPageGoneError("crashed"))

    def unsubscribe(self) -> None:
        self.page.remove_listener("console", self.on_console)
        self.page.remove_listener("close", self.on_close)
        self.page.remove_listener("crash", self.on_crash)


async def _race(
    impl_page: PageImpl,
    source: str,
    args: list[object],
    token: str,
) -> ConsoleMessageImpl:
    """Run the start evaluation and await the outcome concurrently."""
    outcome = _Outcome(impl_page, token)
    start = asyncio.get_running_loop().create_task(impl_page.evaluate(source, args))
    try:
        await asyncio.wait({outcome.future, start}, return_when=asyncio.FIRST_COMPLETED)
        if not outcome.future.done():
            start.result()
        return await outcome.future
    finally:
        # Runs at the deadline too (cancellation): later outcomes go unseen.
        outcome.unsubscribe()
        if start.done():
            _retrieve(start)
        else:
            start.add_done_callback(_retrieve)


async def _within(
    milliseconds: float, work: Awaitable[ConsoleMessageImpl]
) -> ConsoleMessageImpl:
    """Bound ``work`` by a timer in the driver loop; ``0`` means no bound."""
    try:
        async with asyncio.timeout(milliseconds / 1000 if milliseconds > 0 else None):
            return await work
    except TimeoutError as error:
        raise WaitTimeoutError(milliseconds) from error


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
    impl_page: PageImpl = page._impl_obj
    settings = impl_page._timeout_settings
    resolved = settings.timeout() if timeout is None else settings.timeout(timeout)
    token = f"clinic-os-wait-{secrets.token_hex(8)}"
    source = f"{_WATCH_HEAD}{expression}{_WATCH_TAIL}"
    message: ConsoleMessageImpl = page._sync(
        _within(resolved, _race(impl_page, source, [token, arg, resolved], token))
    )
    failed = f"{token}:error"
    if message.text.startswith(failed):
        raise WaitPredicateError(message.text.removeprefix(failed).strip())
    return cast("JSHandle", mapping.from_impl(message.args[1]))
