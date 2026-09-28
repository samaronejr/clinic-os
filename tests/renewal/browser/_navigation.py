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

Every navigation the suites start therefore goes through this module, and
each helper settles the worker (``settle_service_worker``: wait, through the
registration's own ``statechange``/``updatefound`` events, until nothing is
installing, waiting or activating) before it starts the navigation and again
once the new document has loaded:

* ``expect_document`` / ``click_to_navigate``: presses, submits, keys;
* ``goto_settled``, ``reload_settled``, ``go_back_settled``,
  ``go_forward_settled``: the browser-initiated ones (a ``goto`` into scope
  is held exactly like a POST; gate review B1);
* ``wait_for_signed_in``: every sign-in ends on the first signed-in page,
  where the context's worker first registers, and settles there.

After the first settle in a context no later navigation can meet an
installing or activating worker: the worker script is content-versioned and
does not change during a run, so update checks never install. What the
settle removes is the race between a navigation and a normal (milliseconds
long) activation. It cannot shorten an activation: one that genuinely stalls
for the whole timeout now fails loudly at the precondition
(``EvaluateTimeoutError``, nothing clicked or navigated) instead of hanging
inside a click or ``goto`` whose request never left the browser.

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

tests/renewal/test_browser_runner.py guards the suites, failing closed:
only this module may reach a navigating Page/Frame method, however it is
spelled or looked up; Enter, typed newlines, ``dispatch_event`` and in-page
submits, clicks or ``location``/``history`` moves belong inside
``expect_document``; nothing may pass ``no_wait_after``; and a URL wait must
directly follow a settled navigation. Its boundary: a navigating click whose
navigation nothing waits for is indistinguishable from any other click. That
is safe once the context has settled (above).
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
    settle_service_worker(page, timeout=timeout)


def goto_settled(
    page: Page,
    url: str,
    *,
    wait_until: LoadState | None = None,
    timeout: float | None = None,
) -> Response | None:
    """``page.goto`` between two worker settles (see the module docstring)."""
    settle_service_worker(page, timeout=timeout)
    response = page.goto(url, wait_until=wait_until, timeout=timeout)
    settle_service_worker(page, timeout=timeout)
    return response


def reload_settled(
    page: Page,
    *,
    wait_until: LoadState | None = None,
    timeout: float | None = None,
) -> Response | None:
    """``page.reload`` between two worker settles."""
    settle_service_worker(page, timeout=timeout)
    response = page.reload(wait_until=wait_until, timeout=timeout)
    settle_service_worker(page, timeout=timeout)
    return response


def go_back_settled(page: Page, *, timeout: float | None = None) -> Response | None:
    """``page.go_back`` between two worker settles."""
    settle_service_worker(page, timeout=timeout)
    response = page.go_back(timeout=timeout)
    settle_service_worker(page, timeout=timeout)
    return response


def go_forward_settled(page: Page, *, timeout: float | None = None) -> Response | None:
    """``page.go_forward`` between two worker settles."""
    settle_service_worker(page, timeout=timeout)
    response = page.go_forward(timeout=timeout)
    settle_service_worker(page, timeout=timeout)
    return response


def wait_for_signed_in(page: Page, url: UrlMatch = "**/auth/protected/") -> None:
    """End a sign-in: the first signed-in page, with its new worker settled.

    That page is where the worker first registers in a context; after this no
    later navigation of the context can meet an installing or activating
    worker, because the worker script is content-versioned and constant for
    the run (apps/core/views.py ``shell_static_version``).
    """
    page.wait_for_url(url)
    settle_service_worker(page)


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
