"""Document navigations the suites trigger, started after the worker settles.

Every signed-in page carries ``data-service-worker`` and the shell registers
the app's worker on ``load`` (static/js/clinic-os-shell.js). A context's
first signed-in page therefore installs it in the background, and its
activation ends with ``clients.claim()`` (templates/clinic-os-sw.js). While a
registration's active worker is still ``activating``, every engine holds a
navigation into its scope: the request is not sent until activation ends
(Service Workers, Handle Fetch; reproduction on Chromium, Firefox and WebKit:
fix-a12/sw-activation-hold-probe.txt). A click that submits a form then
reports "click action done - waiting for scheduled navigations to finish"
with no request on the wire. Hosted retention@firefox run 36349167980 failed
with that signature on the ``open`` press right after the physician's first
signed-in page, while the agenda load raced the worker's precache (its
worker state was not recorded there).

So ``expect_document`` first waits, through the registration's own
``statechange``/``updatefound`` events, until no worker is installing,
waiting or activating, and only then subscribes to the navigation and hands
control to the caller. A worker that never settles fails that precondition at
the timeout with nothing clicked, instead of inside a click whose request
never left the browser.

The click keeps Playwright's own post-action wait. It does not deadlock with
``expect_navigation``: that is a client-side subscription to the frame's
navigation events, while the click's wait is the driver's signal barrier, and
neither blocks the other (Playwright 1.61 coreBundle ``SignalBarrier``;
_impl/_frame.py ``expect_navigation``). ``no_wait_after=True`` is not a
neutral way to "wait once" either: in 1.61 it also skips reading the
hit-target check (``_performPointerAction``: ``if (options.waitAfter !==
false)``), so a click another element intercepts can be lost instead of
retried. With it, clinic-settings@firefox [768]/[375] and
prescription-draft@firefox [768] lost their submit and timed out waiting for
the navigation (fix-a12 all-suites run).

Only this module may call ``expect_navigation``, and no pointer or key action
inside ``expect_document`` may pass ``no_wait_after``
(tests/renewal/test_browser_runner.py guards both).
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Final, Literal

from renewal.browser._page_wait import click_when_hittable, evaluate_js

if TYPE_CHECKING:
    import re
    from collections.abc import Callable, Iterator

    from playwright._impl._sync_base import EventInfo
    from playwright.sync_api import Locator, Page, Response

# Resolves once the page's registration has an ``activated`` worker and
# nothing installing or waiting. A page that registers the app worker waits
# for ``ready`` first, so a registration still being created is not missed.
SETTLE_WORKER_JS: Final = """async () => {
  if (!("serviceWorker" in navigator)) return "unsupported";
  const container = navigator.serviceWorker;
  const registration = document.documentElement.hasAttribute("data-service-worker")
    ? await container.ready : await container.getRegistration();
  if (!registration) return "unregistered";
  for (;;) {
    const active = registration.active;
    const pending = registration.installing || registration.waiting ||
      (active && active.state !== "activated" ? active : null);
    if (pending) {
      await new Promise((resolve) =>
        pending.addEventListener("statechange", resolve, {once: true}));
    } else if (!active) {
      await new Promise((resolve) =>
        registration.addEventListener("updatefound", resolve, {once: true}));
    } else {
      return active.state;
    }
  }
}"""

type LoadState = Literal["commit", "domcontentloaded", "load", "networkidle"]
type UrlMatch = str | re.Pattern[str] | Callable[[str], bool]


def settle_service_worker(page: Page, *, timeout: float | None = None) -> str:
    """Wait until no service worker for ``page`` is installing or activating.

    Returns the settled state (``activated``, ``unregistered``,
    ``unsupported``) or ``not applicable`` for a context whose pages cannot
    register one: JavaScript disabled (the promise would never settle there)
    or service workers blocked. ``timeout`` is in milliseconds; ``None`` uses
    the page's default timeout.
    """
    # Playwright 1.61 keeps the context's creation options on the impl object.
    options = page.context._impl_obj._options
    if options.get("javaScriptEnabled") is False:
        return "not applicable"
    if options.get("serviceWorkers") == "block":
        return "not applicable"
    return str(evaluate_js(page, SETTLE_WORKER_JS, timeout=timeout))


@contextmanager
def expect_document(
    page: Page,
    *,
    url: UrlMatch | None = None,
    wait_until: LoadState | None = None,
    timeout: float | None = None,
) -> Iterator[EventInfo[Response]]:
    """``page.expect_navigation`` entered only after the worker settled.

    ``timeout`` bounds each of the two waits; ``None`` uses the page's
    default timeout.
    """
    settle_service_worker(page, timeout=timeout)
    with page.expect_navigation(
        url=url, wait_until=wait_until, timeout=timeout
    ) as navigation:
        yield navigation


def click_to_navigate(
    locator: Locator,
    *,
    url: UrlMatch | None = None,
    wait_until: LoadState | None = None,
    timeout: float | None = None,
    hittable: bool = False,
) -> Response | None:
    """Click ``locator`` and wait for the document it navigates to.

    ``hittable`` clicks through ``click_when_hittable`` (a target far outside
    the viewport). Returns the main resource response, as
    ``expect_navigation`` does.
    """
    with expect_document(
        locator.page, url=url, wait_until=wait_until, timeout=timeout
    ) as navigation:
        if hittable:
            click_when_hittable(locator)
        else:
            locator.click()
    return navigation.value
