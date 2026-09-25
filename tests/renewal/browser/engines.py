"""Browser engine selection and mobile emulation profiles for runner suites.

The renewal runner exports ``CLINIC_BROWSER_ENGINE`` (``chromium`` by default,
keeping CI parity) and the resolved executable for that engine. An unknown
engine fails the suite; it never skips and never falls back to Chromium.

Mobile profiles mirror Playwright 1.61's ``iPhone 15`` and ``Pixel 8``
device descriptors (viewport, device scale factor, user agent) with touch
enabled, so a suite can assert touch-first layouts on every engine.

Two browser capabilities are not uniform across engines in the pinned
Playwright 1.61.0, so this module owns the one place each differs:

* Camera/microphone. Chromium runs real fake capture devices
  (``--use-fake-device-for-media-stream``) behind its real permission model.
  Playwright cannot grant ``camera``/``microphone`` on Firefox or WebKit
  (``BrowserContext.grant_permissions`` raises "Unknown permission: camera";
  https://github.com/microsoft/playwright/issues/20563 and
  https://github.com/microsoft/playwright/issues/7635; WebKit support landed
  only with WebKit r2332 in https://github.com/microsoft/playwright/pull/41836,
  newer than the pinned webkit-2311). There, ``install_media`` replaces
  ``getUserMedia`` with a synthetic source: live canvas and oscillator
  tracks, delivered through a loopback peer connection and gated by a
  context-level permission that ``grant_media`` flips at runtime. A denied
  request rejects with ``NotAllowedError``, as a browser denial does.
  ``END_TRACK_JS`` ends the audio track like an unplugged device: a genuine
  source end on Firefox and WebKit, and a dispatched ``ended`` on
  Chromium's fake device, which cannot be unplugged.
* Clipboard. Chromium needs ``clipboard-read``/``clipboard-write`` grants.
  Firefox has no clipboard permissions in Playwright; WebKit maps only
  ``clipboard-read``, and its ``readText()`` still needs a user gesture.
  Every engine writes the clipboard on a user click. ``pasted_clipboard``
  reads it back with a real paste keystroke, which works on all three.
* Cancelled fetches. A navigation cancels an in-flight fetch on every
  engine (https://github.com/microsoft/playwright/issues/42823). WebKit
  also reports the cancellation of a same-origin fetch as a page error
  ("Fetch API cannot load <url> due to access control checks."), even when
  the page handles the rejection. ``watch_page_errors`` drops exactly that
  WebKit report for same-origin URLs and records everything else.
* Console. Chromium and WebKit log a failed HTTP response as "Failed to
  load resource: the server responded with a status of N (...)"; Firefox
  logs nothing (``logs_failed_responses``). A request refused by
  ``BrowserContext.set_offline`` is logged as ``net::ERR_INTERNET_DISCONNECTED``
  by Chromium and as "WebKit encountered an internal error" by WebKit
  (``offline_console``). Aborted routes are only ``info`` on WebKit.
* Request interception. On WebKit, ``page.route`` does not see requests
  from a page controlled by a fetch-handling service worker, even ones the
  worker passes through (the app's worker handles static GETs only). Suites
  that intercept requests open their contexts with
  ``service_workers="block"`` (https://playwright.dev/python/docs/api/class-page#page-route).
* Session history. On Firefox, Playwright's ``page.reload()`` corrupts
  session history: a later ``go_back()`` reloads the current entry instead
  of returning. It also sometimes never reports ``load`` for a page whose
  subrequests are being routed. The document's own ``location.reload()`` and
  ``history.back()`` walk the same history as the browser buttons on every
  engine, so journeys that reload and then go back use those. Firefox does
  not restore form controls on Back under Playwright, which disables its
  bfcache
  (https://github.com/microsoft/playwright/blob/main/browser_patches/firefox/preferences/playwright.cfg),
  so ``restores_forms_on_back`` gates the one scenario that resubmits a
  restored form.
* Clipped video. Playwright's headless WebKit (webkit-2311, WPE) never
  displays a MediaStream ``<video>`` inside an ``overflow: hidden`` box, so
  its ``readyState`` stays HAVE_NOTHING. Chromium and Firefox display it, and
  so does WebKit without the clip (reproduction:
  .omo/evidence/clinic-ops-premium-intelligence/task-14/fix1/webkit-clipped-mediastream-probe.txt).
  ``displays_clipped_video`` gates only the painted-first-frame check; on
  WebKit the element must still be playing a live camera track.
* 200% zoom. ``zoom_200`` opens the page in a context with a halved viewport
  (640x450) at device scale factor 2. This proxy works on every engine.
  Real browser zoom has no Playwright API
  (https://github.com/microsoft/playwright/issues/2497), so
  ``browser_zoom_200`` uses Chromium's own zoom setting
  (``chrome.settingsPrivate``), Firefox's ``layout.css.devPixelsPerPx`` pref
  and the proxy on WebKit. All three give the same 640x450 CSS viewport at
  devicePixelRatio 2.
  ``zoom_screenshot`` captures a real-zoom page through DevTools, because
  Playwright's full-page clip is in CSS pixels.
"""

from __future__ import annotations

import base64
import os
import re
import weakref
from contextlib import contextmanager
from dataclasses import dataclass
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Final
from urllib.parse import urlsplit

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from playwright.sync_api import (
        Browser,
        BrowserContext,
        Error,
        Page,
        Playwright,
    )

ENGINES: Final = ("chromium", "firefox", "webkit")
DEFAULT_ENGINE: Final = "chromium"
CHROMIUM_ARGS: Final = ("--no-sandbox", "--disable-dev-shm-usage")
CHROMIUM_FAKE_MEDIA_ARG: Final = "--use-fake-device-for-media-stream"
MEDIA_PERMISSIONS: Final = ("camera", "microphone")
CLIPBOARD_PERMISSIONS: Final = ("clipboard-read", "clipboard-write")
PASTE_TARGET_JS: Final = """() => {
  const target = document.createElement('textarea');
  target.setAttribute('data-test-paste-target', '');
  document.body.append(target);
  target.focus();
}"""
PASTED_JS: Final = """() => {
  const target = document.querySelector('[data-test-paste-target]');
  const value = target.value;
  target.remove();
  return value;
}"""
ZOOM_200_SCALE: Final = 2
CHROME_SET_ZOOM_JS: Final = """(factor) => new Promise((resolve, reject) => {
  chrome.settingsPrivate.setDefaultZoom(factor, () => {
    if (chrome.runtime.lastError) {
      reject(new Error(chrome.runtime.lastError.message));
    } else { resolve(); }
  });
})"""
CHROME_GET_ZOOM_JS: Final = """() => new Promise(resolve =>
  chrome.settingsPrivate.getDefaultZoom(resolve))"""
SYNTHETIC_MEDIA_BINDING: Final = "__clinicSyntheticMediaGranted"
# Replaces getUserMedia with live synthetic tracks: a repainting canvas for
# video (it paints its first frame at once) and an oscillator for audio,
# delivered through a loopback RTCPeerConnection. They are real
# MediaStreamTracks, so enabled, readyState and stop() behave as they do for
# devices, and __clinicSyntheticMedia.end(audioTrack) ends the audio the way
# an unplugged microphone does: the source stops and the track fires a
# genuine "ended". Firefox never delivers a script-dispatched "ended" to a
# MediaStreamTrack listener. Browsers exempt real capture streams from
# autoplay restrictions; WebKit does not know these streams are capture
# streams, so an autoplay element given one is started explicitly, as a
# capture stream would be. WebKit also never renders a stream assigned to a
# <video> un-hidden in the same task before any layout (the patient device
# test does this); the element is laid out first. Whether real Safari does
# the same with a real camera is left to docs/qa/real-device-protocol.md.
SYNTHETIC_MEDIA_JS: Final = """(() => {
  // Patch the prototype: WebKit can hand the page a navigator.mediaDevices
  // instance that never saw an own-property override made at document start.
  const proto = window.MediaDevices && MediaDevices.prototype;
  if (!proto) return;
  const endings = new WeakMap();
  const captured = new WeakSet();
  const srcObject = Object.getOwnPropertyDescriptor(
    HTMLMediaElement.prototype, 'srcObject');
  Object.defineProperty(HTMLMediaElement.prototype, 'srcObject', {
    ...srcObject,
    set(value) {
      const synthetic = Boolean(value) && captured.has(value);
      if (synthetic) this.getBoundingClientRect();
      srcObject.set.call(this, value);
      if (synthetic && this.autoplay) {
        this.play().catch(() => {});
      }
    },
  });
  window.__clinicSyntheticMedia = {
    end: (track) => {
      const end = endings.get(track);
      if (!end) throw new Error('not a synthetic media track');
      end();
    },
  };
  const loopback = async (source) => {
    const sender = new RTCPeerConnection();
    const receiver = new RTCPeerConnection();
    const relay = (peer) => (event) =>
      event.candidate && peer.addIceCandidate(event.candidate);
    sender.onicecandidate = relay(receiver);
    receiver.onicecandidate = relay(sender);
    const received = new Promise((resolve) => { receiver.ontrack = resolve; });
    sender.addTrack(source);
    await sender.setLocalDescription(await sender.createOffer());
    await receiver.setRemoteDescription(sender.localDescription);
    await receiver.setLocalDescription(await receiver.createAnswer());
    await sender.setRemoteDescription(receiver.localDescription);
    const {track, transceiver} = await received;
    endings.set(track, () => {
      transceiver.stop();
      source.stop();
      sender.close();
      receiver.close();
    });
    return track;
  };
  const videoSource = () => {
    const canvas = document.createElement('canvas');
    canvas.width = 320;
    canvas.height = 240;
    const paint = canvas.getContext('2d');
    const track = canvas.captureStream(15).getVideoTracks()[0];
    let frame = 0;
    const draw = () => {
      if (track.readyState === 'ended') return;
      paint.fillStyle = frame % 2 ? '#0f2d3a' : '#12303d';
      paint.fillRect(0, 0, canvas.width, canvas.height);
      frame += 1;
      requestAnimationFrame(draw);
    };
    draw();
    return track;
  };
  const audioSource = () => {
    const audio = new AudioContext();
    const tone = audio.createOscillator();
    const sink = audio.createMediaStreamDestination();
    tone.connect(sink);
    tone.start();
    return sink.stream.getAudioTracks()[0];
  };
  proto.getUserMedia = async function getUserMedia(constraints = {}) {
    if (!(await window.__clinicSyntheticMediaGranted())) {
      throw new DOMException('Permission denied', 'NotAllowedError');
    }
    const sources = [];
    if (constraints.video) sources.push(videoSource());
    if (constraints.audio) sources.push(audioSource());
    if (!sources.length) throw new TypeError('audio or video must be requested');
    const tracks = sources.map((source) =>
      source.kind === 'audio' ? loopback(source) : source);
    const stream = new MediaStream(await Promise.all(tracks));
    captured.add(stream);
    return stream;
  };
})();"""
_SYNTHETIC_GRANTS: weakref.WeakKeyDictionary[BrowserContext, dict[str, bool]] = (
    weakref.WeakKeyDictionary()
)


@dataclass(frozen=True, slots=True)
class MobileProfile:
    """One emulated phone: CSS viewport, pixel density and user agent."""

    label: str
    width: int
    height: int
    device_scale_factor: float
    user_agent: str


MOBILE_PROFILES: Final = {
    "iphone-15": MobileProfile(
        label="iPhone 15",
        width=393,
        height=659,
        device_scale_factor=3,
        user_agent=(
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X)"
            " AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.5"
            " Mobile/15E148 Safari/604.1"
        ),
    ),
    "pixel-8": MobileProfile(
        label="Pixel 8",
        width=412,
        height=839,
        device_scale_factor=2.625,
        user_agent=(
            "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36"
            " (KHTML, like Gecko) Chrome/149.0.7827.55 Mobile Safari/537.36"
        ),
    ),
}


def selected_engine() -> str:
    """Return the runner-selected engine; an unknown name fails the suite."""
    engine = os.environ.get("CLINIC_BROWSER_ENGINE", "") or DEFAULT_ENGINE
    if engine not in ENGINES:
        pytest.fail(f"CLINIC_BROWSER_ENGINE names an unsupported engine: {engine}")
    return engine


END_TRACK_JS: Final = """() => {
  const stream = document.querySelector('[data-self-video]').srcObject;
  const track = stream.getAudioTracks()[0];
  if (window.__clinicSyntheticMedia) {
    window.__clinicSyntheticMedia.end(track);
  } else {
    track.dispatchEvent(new Event('ended'));
  }
}"""


def launch(
    driver: Playwright, engine: str, executable: str, *, media: bool = False
) -> Browser:
    """Launch ``engine`` from the runner-resolved executable, never another."""
    if engine == "chromium":
        args = [*CHROMIUM_ARGS, *([CHROMIUM_FAKE_MEDIA_ARG] if media else [])]
        return driver.chromium.launch(executable_path=executable, args=args)
    browser_type = {"firefox": driver.firefox, "webkit": driver.webkit}[engine]
    return browser_type.launch(executable_path=executable)


def launch_selected(driver: Playwright, *, media: bool = False) -> Browser:
    """Launch the runner-selected engine for a suite that owns its browser."""
    return launch(
        driver,
        selected_engine(),
        os.environ["CLINIC_RENEWAL_BROWSER_EXECUTABLE"],
        media=media,
    )


def _engine_of(context: BrowserContext) -> str:
    browser = context.browser
    assert browser is not None
    return browser.browser_type.name


def install_media(context: BrowserContext, origin: str, *, granted: bool) -> None:
    """Set a fresh context's camera/microphone permission (before any page)."""
    if _engine_of(context) == "chromium":
        if granted:
            context.grant_permissions(list(MEDIA_PERMISSIONS), origin=origin)
        return
    state = {"granted": granted}
    _SYNTHETIC_GRANTS[context] = state
    context.expose_function(SYNTHETIC_MEDIA_BINDING, lambda: state["granted"])
    context.add_init_script(SYNTHETIC_MEDIA_JS)


def grant_media(context: BrowserContext, origin: str) -> None:
    """Grant camera/microphone later, as a user fixing the permission would."""
    if _engine_of(context) == "chromium":
        context.grant_permissions(list(MEDIA_PERMISSIONS), origin=origin)
        return
    _SYNTHETIC_GRANTS[context]["granted"] = True


def media_source(context: BrowserContext) -> str:
    """Describe the capture source a context's media comes from (evidence)."""
    if _engine_of(context) == "chromium":
        return f"chromium {CHROMIUM_FAKE_MEDIA_ARG}"
    return f"{_engine_of(context)} synthetic canvas/oscillator getUserMedia"


OFFLINE_CONSOLE: Final = {
    "chromium": "net::ERR_INTERNET_DISCONNECTED",
    "webkit": "WebKit encountered an internal error",
}


def logs_failed_responses(context: BrowserContext) -> bool:
    """Whether the engine logs a failed HTTP response to the console."""
    return _engine_of(context) != "firefox"


def displays_clipped_video(context: BrowserContext) -> bool:
    """Whether a clipped MediaStream <video> reaches HAVE_CURRENT_DATA."""
    return _engine_of(context) != "webkit"


def restores_forms_on_back(context: BrowserContext) -> bool:
    """Whether Back puts a form's typed/selected values back on this engine."""
    return _engine_of(context) != "firefox"


def history_reload(page: Page) -> None:
    """Reload the way the document's own Reload does (see Session history)."""
    with page.expect_navigation():
        page.evaluate("location.reload()")


def history_back(page: Page, url: str) -> None:
    """Go Back the way the document's own Back does, landing on ``url``."""
    with page.expect_navigation(url=url):
        page.evaluate("history.back()")


def offline_console(context: BrowserContext) -> str | None:
    """The console text for a request refused offline, if the engine logs one."""
    return OFFLINE_CONSOLE.get(_engine_of(context))


WEBKIT_CANCELLED_FETCH: Final = re.compile(
    r"Fetch API cannot load (?P<url>\S+) due to access control checks\."
)


def watch_page_errors(page: Page, sink: list[str]) -> None:
    """Append ``page``'s uncaught errors to ``sink`` (see module docstring)."""
    webkit = _engine_of(page.context) == "webkit"

    def record(error: Error) -> None:
        first_line = (error.stack or "").split("\n", 1)[0]
        cancelled = WEBKIT_CANCELLED_FETCH.fullmatch(first_line) if webkit else None
        if cancelled is not None and (
            urlsplit(cancelled["url"]).netloc == urlsplit(page.url).netloc
        ):
            return
        sink.append(str(error))

    page.on("pageerror", record)


def grant_clipboard(context: BrowserContext) -> None:
    """Let a user click copy to the clipboard on the context's engine."""
    if _engine_of(context) == "chromium":
        context.grant_permissions(list(CLIPBOARD_PERMISSIONS))


def pasted_clipboard(page: Page) -> str:
    """Return the clipboard text as a real paste keystroke delivers it."""
    page.evaluate(PASTE_TARGET_JS)
    page.keyboard.press("ControlOrMeta+V")
    value: str = page.evaluate(PASTED_JS)
    return value


def zoom_200(
    page: Page, *, java_script_enabled: bool = True
) -> tuple[BrowserContext, Page]:
    """Open a 200% zoom proxy page sharing ``page``'s signed-in session.

    The proxy page has loaded ``page.url`` once; the caller navigates it
    again after attaching its own listeners. Firefox drops the device scale
    factor when a ``Cross-Origin-Opener-Policy: same-origin`` response (the
    app's default) swaps the browsing context group on that first load.
    Re-applying the viewport restores it for every later navigation.
    """
    browser = page.context.browser
    assert browser is not None
    context = browser.new_context(
        locale="pt-BR",
        viewport={"width": 640, "height": 450},
        device_scale_factor=ZOOM_200_SCALE,
        java_script_enabled=java_script_enabled,
        storage_state=page.context.storage_state(),
    )
    zoomed = context.new_page()
    zoomed.goto(page.url)
    zoomed.set_viewport_size({"width": 640, "height": 450})
    return context, zoomed


def mobile_context(
    browser: Browser, profile: str, *, locale: str = "pt-BR"
) -> BrowserContext:
    """Open a touch-enabled context emulating one of ``MOBILE_PROFILES``."""
    device = MOBILE_PROFILES[profile]
    return browser.new_context(
        viewport={"width": device.width, "height": device.height},
        device_scale_factor=device.device_scale_factor,
        user_agent=device.user_agent,
        is_mobile=True,
        has_touch=True,
        locale=locale,
    )


@contextmanager
def browser_zoom_200(page: Page, profile_root: Path) -> Iterator[tuple[Page, str]]:
    """Yield a blank page, signed in like ``page``, at 200% zoom, plus the method.

    Navigate the yielded page after attaching listeners. Chromium applies its
    real default-zoom setting in a throwaway profile under ``profile_root``.
    Playwright has no browser zoom API
    (https://github.com/microsoft/playwright/issues/2497), so Firefox scales
    through its own ``layout.css.devPixelsPerPx`` pref in a throwaway profile.
    That pref survives every navigation, whereas the ``zoom_200`` proxy's
    scale factor is lost on each COOP browsing-context swap, such as a form
    POST. WebKit uses the ``zoom_200`` proxy.
    """
    browser = page.context.browser
    assert browser is not None
    engine = _engine_of(page.context)
    if engine == "firefox":
        with TemporaryDirectory(prefix="zoom-profile-", dir=profile_root) as profile:
            context = browser.browser_type.launch_persistent_context(
                profile,
                executable_path=os.environ["CLINIC_RENEWAL_BROWSER_EXECUTABLE"],
                headless=True,
                locale="pt-BR",
                viewport={"width": 640, "height": 450},
                firefox_user_prefs={"layout.css.devPixelsPerPx": "2.0"},
            )
            try:
                context.set_storage_state(page.context.storage_state())
                yield context.pages[0], "firefox layout.css.devPixelsPerPx=2 at 640x450"
            finally:
                context.close()
        return
    if engine == "webkit":
        context, zoomed = zoom_200(page)
        try:
            yield zoomed, "device_scale_factor=2 at 640x450 (Playwright #2497)"
        finally:
            context.close()
        return
    with TemporaryDirectory(prefix="zoom-profile-", dir=profile_root) as profile:
        context = browser.browser_type.launch_persistent_context(
            profile,
            executable_path=os.environ["CLINIC_RENEWAL_BROWSER_EXECUTABLE"],
            headless=True,
            locale="pt-BR",
            viewport={"width": 1280, "height": 900},
            args=list(CHROMIUM_ARGS),
        )
        try:
            context.set_storage_state(page.context.storage_state())
            settings = context.new_page()
            settings.goto("chrome://settings/appearance")
            settings.evaluate(CHROME_SET_ZOOM_JS, ZOOM_200_SCALE)
            assert settings.evaluate(CHROME_GET_ZOOM_JS) == ZOOM_200_SCALE
            settings.close()
            yield context.pages[0], "chrome.settingsPrivate.setDefaultZoom"
        finally:
            context.close()


def zoom_screenshot(page: Page, destination: Path) -> None:
    """Write a full-page capture of a ``browser_zoom_200`` page."""
    if _engine_of(page.context) != "chromium":
        page.screenshot(path=str(destination), full_page=True)
        return
    # Playwright's full_page clips to CSS pixels even at native browser zoom;
    # DevTools' screenshot clip uses device-independent pixels.
    session = page.context.new_cdp_session(page)
    try:
        layout = session.send("Page.getLayoutMetrics")
        assert layout["cssVisualViewport"]["zoom"] == ZOOM_200_SCALE
        screenshot = session.send(
            "Page.captureScreenshot",
            {
                "captureBeyondViewport": True,
                "clip": {**layout["contentSize"], "scale": 1},
            },
        )
        destination.write_bytes(base64.b64decode(screenshot["data"]))
    finally:
        session.detach()
