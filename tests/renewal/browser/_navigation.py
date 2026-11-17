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
* ``goto_refused``: a navigation the engine must refuse (``set_offline``,
  an aborted route); see below;
* ``wait_for_signed_in``: every sign-in ends on the first signed-in page,
  where the context's worker first registers, and settles there;
* ``wait_for_document``: a document the suite did not navigate to itself,
  such as the one a popup was opened to.

A refused navigation has a second hazard beside the worker hold. Firefox
loads an error document for it that commits under the refused URL itself
(``document.title`` "Problem loading page", a ``framenavigated`` to the
target with no request on the wire); Chromium loads its own
``chrome-error://`` document. That commit can outlive the ``Page.goto``
error by tens of milliseconds and then interrupt the next navigation: a
hosted workspace@firefox run failed ``Page.goto`` right after
``set_offline(offline=False)`` with "Navigation to <patients> is
interrupted by another navigation to <patients>" (fix-a14; event trace in
the fix-a14 QA evidence, reproduced in the suite and a minimal driver).
The refusal is not the end of the navigation: ``goto_refused`` arms a
main-frame ``framenavigated`` subscription *before* ``goto``, lets the
refusal raise, then drains the error document's commit - armed early, the
event is captured whether it lands before or after the refusal - so
nothing pending can interrupt the recovery navigation. WebKit loads no error document at
all; draining there would wait out the timeout for nothing, so the helper
skips the subscription on engines that never commit one.

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

import weakref
from contextlib import ExitStack, contextmanager
from typing import TYPE_CHECKING, Final, Literal

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from renewal.browser._page_wait import (
    EvaluateTimeoutError,
    click_when_hittable,
    evaluate_js,
)

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
  const registered = document.documentElement.hasAttribute("data-service-worker");
  window.__clinicSettleWait = registered ? "ready" : "getRegistration";
  const registration = registered
    ? await container.ready : await container.getRegistration();
  if (!registration) return "unregistered";
  for (;;) {
    const active = registration.active;
    const pending = registration.installing || registration.waiting ||
      (active && active.state !== "activated" ? active : null);
    if (pending) {
      const which = registration.installing === pending ? "installing"
        : registration.waiting === pending ? "waiting" : "active";
      window.__clinicSettleWait = "statechange of " + which + " " + pending.state;
      await new Promise((resolve) =>
        pending.addEventListener("statechange", resolve, {once: true}));
    } else if (!active) {
      window.__clinicSettleWait = "updatefound (no active worker)";
      await new Promise((resolve) =>
        registration.addEventListener("updatefound", resolve, {once: true}));
    } else {
      delete window.__clinicSettleWait;
      return active.state;
    }
  }
}"""
SETTLE_WAIT_JS: Final = "() => String(window.__clinicSettleWait || 'nothing')"
# Bounds the one read that names a stuck settle; never a wait for the page.
SETTLE_PROBE_MS: Final = 2_000
# Installed before a tracked page's first document (``track_realtime``).
# Counts the htmx requests of realtime-refreshed regions (an ``rt:`` event in
# ``hx-trigger``) from ``htmx:beforeSend`` to ``htmx:afterRequest``, which htmx
# fires on load, error, abort and timeout alike. ``quiet()`` cancels every
# later trigger at ``htmx:confirm`` (before ``hx-sync`` could abort the request
# in flight) and resolves once none is in flight: a navigation started then
# cuts off no refresh, so Firefox and WebKit log no htmx abort or send error.
REALTIME_TRACKER_JS: Final = """(() => {
  const realtime = (element) => element instanceof Element &&
    /(^|[\\s,])rt:/.test(element.getAttribute("hx-trigger") || "");
  let inflight = 0;
  let frozen = false;
  let waiters = [];
  document.addEventListener("htmx:confirm", (event) => {
    if (frozen && realtime(event.detail.elt)) event.preventDefault();
  }, true);
  document.addEventListener("htmx:beforeSend", (event) => {
    if (realtime(event.detail.elt)) inflight += 1;
  }, true);
  document.addEventListener("htmx:afterRequest", (event) => {
    if (!realtime(event.detail.elt)) return;
    inflight = Math.max(0, inflight - 1);
    if (inflight === 0) {
      waiters.forEach((resolve) => resolve(true));
      waiters = [];
    }
  }, true);
  window.__clinicRealtime = {
    quiet() {
      frozen = true;
      return inflight === 0 ? true : new Promise((resolve) => waiters.push(resolve));
    },
    resume() { frozen = false; },
  };
})();"""
QUIET_REALTIME_JS: Final = (
    "() => window.__clinicRealtime ? window.__clinicRealtime.quiet() : true"
)
RESUME_REALTIME_JS: Final = (
    "() => { if (window.__clinicRealtime) window.__clinicRealtime.resume(); }"
)
_REALTIME_PAGES: Final[weakref.WeakSet[Page]] = weakref.WeakSet()

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
    try:
        return str(evaluate_js(page, SETTLE_WORKER_JS, timeout=timeout))
    except EvaluateTimeoutError as error:
        raise SettleTimeoutError(_settle_wait(page)) from error


class SettleTimeoutError(EvaluateTimeoutError):
    """The worker did not settle by the deadline; names what it waited on."""

    def __init__(self, waiting: str) -> None:
        PlaywrightTimeoutError.__init__(
            self, f"service worker did not settle: waiting on {waiting}"
        )
        self.waiting = waiting


def _settle_wait(page: Page) -> str:
    """The branch the stuck settle waits in, or that the page did not answer."""
    try:
        return str(evaluate_js(page, SETTLE_WAIT_JS, timeout=SETTLE_PROBE_MS))
    except EvaluateTimeoutError:
        return "an answer from the page itself (it did not respond)"


def track_realtime(page: Page) -> Page:
    """Quiet ``page``'s realtime htmx refreshes around every helper navigation.

    Call before the page's first navigation. A suite that fails on console
    errors needs it on pages whose realtime regions can refresh mid-navigation.
    """
    page.add_init_script(REALTIME_TRACKER_JS)
    _REALTIME_PAGES.add(page)
    return page


def _before_navigation(page: Page, timeout: float | None) -> None:
    if page in _REALTIME_PAGES:
        evaluate_js(page, QUIET_REALTIME_JS, timeout=timeout)
    settle_service_worker(page, timeout=timeout)


def _after_navigation(page: Page, timeout: float | None) -> None:
    settle_service_worker(page, timeout=timeout)
    if page in _REALTIME_PAGES:
        # A new document starts unfrozen; a surviving one refreshes again.
        evaluate_js(page, RESUME_REALTIME_JS, timeout=timeout)


@contextmanager
def expect_document(
    page: Page,
    *,
    url: UrlMatch | None = None,
    wait_until: LoadState | None = None,
    timeout: float | None = None,
    new_document: bool = False,
) -> Iterator[EventInfo[Response]]:
    """``page.expect_navigation`` entered only after the worker settled.

    ``timeout`` bounds each of the two waits; ``None`` uses the page's
    default timeout. ``new_document`` also awaits the page ``load`` of the
    document that replaces this one: a same-document step that the page
    answers with ``location.reload()`` (htmx's Back on a history-cache miss)
    reports its navigation first, and settling then would evaluate in the
    document the reload is destroying ("Execution context was destroyed").
    """
    _before_navigation(page, timeout)
    with ExitStack() as stack:
        if new_document:
            stack.enter_context(page.expect_event("load", timeout=timeout))
        with page.expect_navigation(
            url=url, wait_until=wait_until, timeout=timeout
        ) as navigation:
            yield navigation
    _after_navigation(page, timeout)


def goto_settled(
    page: Page,
    url: str,
    *,
    wait_until: LoadState | None = None,
    timeout: float | None = None,
) -> Response | None:
    """``page.goto`` between two worker settles (see the module docstring)."""
    _before_navigation(page, timeout)
    response = page.goto(url, wait_until=wait_until, timeout=timeout)
    _after_navigation(page, timeout)
    return response


# Engines that commit an error document for a refused navigation. WebKit's
# refusal leaves the previous document and emits no framenavigated, so a
# drain subscription there would wait out the whole timeout.
ERROR_DOCUMENT_ENGINES: Final = frozenset({"chromium", "firefox"})


class UnexpectedNavigationError(PlaywrightError):
    """``goto_refused`` saw the navigation it expected to fail succeed."""

    def __init__(self, url: str) -> None:
        super().__init__(
            f"goto_refused expected a refused navigation, but {url} loaded"
        )


def goto_refused(
    page: Page,
    url: str,
    *,
    timeout: float | None = None,
) -> None:
    """``page.goto`` that must fail, drained of the engine's error document.

    Refused navigations (offline, aborted routes) still navigate: Firefox
    commits an error document under the refused URL, Chromium commits
    ``chrome-error://`` (see the module docstring). The subscription arms
    before ``goto``, so the commit is captured whenever it lands - before
    the refusal returns or after. The refusal is re-raised for the
    caller's ``pytest.raises``; a navigation that unexpectedly succeeds
    raises ``UnexpectedNavigationError``. On engines without an error
    document the refusal is re-raised immediately. ``timeout`` is in
    milliseconds, for both the ``goto`` and the drain; ``None`` uses the
    page's default.
    """
    settle_service_worker(page, timeout=timeout)
    browser = page.context.browser
    drains = browser is not None and browser.browser_type.name in (
        ERROR_DOCUMENT_ENGINES
    )
    refusal: PlaywrightError | None = None
    if drains:
        with page.expect_event(
            "framenavigated",
            predicate=lambda frame: frame == page.main_frame,
            timeout=timeout,
        ):
            try:
                page.goto(url, timeout=timeout)
            except PlaywrightError as error:
                refusal = error
    else:
        try:
            page.goto(url, timeout=timeout)
        except PlaywrightError as error:
            refusal = error
    if refusal is None:
        raise UnexpectedNavigationError(url)
    raise refusal


def reload_settled(
    page: Page,
    *,
    wait_until: LoadState | None = None,
    timeout: float | None = None,
) -> Response | None:
    """``page.reload`` between two worker settles."""
    _before_navigation(page, timeout)
    response = page.reload(wait_until=wait_until, timeout=timeout)
    _after_navigation(page, timeout)
    return response


def go_back_settled(page: Page, *, timeout: float | None = None) -> Response | None:
    """``page.go_back`` between two worker settles."""
    _before_navigation(page, timeout)
    response = page.go_back(timeout=timeout)
    _after_navigation(page, timeout)
    return response


def go_forward_settled(page: Page, *, timeout: float | None = None) -> Response | None:
    """``page.go_forward`` between two worker settles."""
    _before_navigation(page, timeout)
    response = page.go_forward(timeout=timeout)
    _after_navigation(page, timeout)
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


def wait_for_document(page: Page, url: UrlMatch) -> None:
    """Settle ``url`` once it has committed and loaded in ``page``.

    For a document the suite did not navigate to itself: a popup is reported
    on its initial ``about:blank``, before the document it was opened to
    commits, and evaluating there races that commit (Chromium: "Execution
    context was destroyed").
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
