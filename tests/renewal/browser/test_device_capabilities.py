"""Device capability study (ADR-013, todo 69): L1 emulation on every engine.

Four criteria decide whether the browser suffices without a native companion:
background audio, push, camera document capture and the WebAuthn platform
authenticator. Pages are served from a private loopback origin under a strict
CSP. Playwright cannot lock a screen, reach a real push service or attach a
real camera or authenticator to Firefox/WebKit, so those subchecks are
reported ``unmeasurable`` and never ``pass``. Nothing here is device proof:
EG-14 real-device runs (docs/qa/real-device-protocol.md) stay pending.
"""

from __future__ import annotations

import http.server
import json
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from playwright.sync_api import sync_playwright

from renewal.browser._navigation import goto_settled
from renewal.browser._page_wait import evaluate_js, wait_for_js
from renewal.browser.device_capability_report import (
    CRITERIA,
    DEVICE_PROOF,
    EVIDENCE_LEVEL,
    criterion_status,
    validate_engine_report,
)
from renewal.browser.engines import (
    deliver_push,
    grant_notifications,
    install_media,
    launch_selected,
    media_source,
    new_context,
    virtual_authenticator,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from playwright.sync_api import Page

PAGES: Final = Path(__file__).parent / "device_capabilities"
CSP: Final = (
    "default-src 'none'; script-src 'self'; style-src 'self'; worker-src 'self'; "
    "media-src 'self' blob:; img-src 'self' blob: data:; connect-src 'self'"
)
MIN_WIDTH: Final = 1280
MIN_HEIGHT: Final = 720
Sub = dict[str, str]


class _Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        super().__init__(*args, directory=str(PAGES), **kwargs)

    def end_headers(self) -> None:
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        del format, args


@contextmanager
def _serve() -> Iterator[str]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://localhost:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _num(seen: dict[str, object], key: str) -> float:
    value = seen.get(key)
    return float(value) if isinstance(value, int | float) else 0.0


def _sub(name: str, result: str, detail: str) -> Sub:
    return {"name": name, "result": result, "detail": detail}


def _verdict(*, ok: bool, detail: str) -> tuple[str, str]:
    return ("pass" if ok else "fail", detail)


def _run(page: Page, name: str) -> dict[str, object]:
    page.locator(f'button[data-probe="{name}"]').click()
    wait_for_js(page, f"window.deviceCapabilities.results.{name} !== undefined")
    result: dict[str, object] = evaluate_js(
        page, f"window.deviceCapabilities.results.{name}"
    )
    return result


def _audio(page: Page) -> list[Sub]:
    seen = _run(page, "audio")
    checks = [
        _sub(
            "playback_advances",
            *_verdict(
                ok=bool(seen.get("advances")), detail=f"error={seen.get('error')}"
            ),
        ),
        _sub(
            "media_session",
            *_verdict(
                ok=bool(seen.get("media_session"))
                and bool(seen.get("media_session_handler")),
                detail=f"state={seen.get('media_session_state')}",
            ),
        ),
    ]
    other = page.context.new_page()
    other.bring_to_front()
    after = evaluate_js(page, "window.deviceCapabilities.audioAfterBackground()")
    other.close()
    if after["background_emulated"]:
        checks.append(
            _sub(
                "background_tab",
                *_verdict(
                    ok=bool(after["advances_in_background"]),
                    detail="page hidden by a foreground tab",
                ),
            )
        )
    else:
        checks.append(
            _sub(
                "background_tab",
                "unmeasurable",
                f"engine keeps visibilityState={after['visibility']} for a background"
                " page in headless emulation",
            )
        )
    checks.append(
        _sub(
            "screen_lock",
            "unmeasurable",
            "emulation cannot lock a screen or let the OS suspend the tab; EG-14 RD-6",
        )
    )
    return checks


def _subscribe_outcome(seen: dict[str, object]) -> str:
    if seen.get("subscribe_error"):
        return str(seen["subscribe_error"])
    return "subscribe() left pending" if seen.get("stalled") else "not attempted"


def _push(page: Page, origin: str, *, engine: str) -> list[Sub]:
    granted = grant_notifications(page.context, origin)
    seen = _run(page, "push")
    missing = [
        key
        for key in ("notification_api", "push_manager", "service_worker")
        if not seen.get(key)
    ]
    if missing and engine == "webkit":
        # Playwright's WebKit is a WPE build, not Safari: an absent API says
        # nothing about Safari, so it is a gap to settle on a device.
        surface = _sub(
            "api_surface",
            "unmeasurable",
            f"Playwright WebKit (WPE) lacks {', '.join(missing)}; "
            "Safari must be checked on a device (EG-14)",
        )
    else:
        surface = _sub(
            "api_surface",
            *_verdict(
                ok=not missing,
                detail="Notification, PushManager, serviceWorker"
                if not missing
                else f"missing {', '.join(missing)}",
            ),
        )
    checks = [
        surface,
        _sub(
            "service_worker_registered",
            *_verdict(
                ok=bool(seen.get("registered")),
                detail=f"push-sw.js active; stalled at {seen.get('step')}"
                if seen.get("stalled")
                else "push-sw.js active",
            ),
        ),
    ]
    if granted:
        checks.append(
            _sub(
                "permission",
                *_verdict(
                    ok=seen.get("permission") == "granted",
                    detail=f"permission={seen.get('permission')}",
                ),
            )
        )
    else:
        checks.append(
            _sub(
                "permission",
                "unmeasurable",
                f"Playwright cannot grant notifications on {engine}",
            )
        )
    if seen.get("subscribed"):
        checks.append(_sub("subscribe", "pass", "subscription endpoint issued"))
    else:
        checks.append(
            _sub(
                "subscribe",
                "unmeasurable",
                "no real push service reachable in emulation: "
                f"{_subscribe_outcome(seen)}",
            )
        )
    evaluate_js(page, "window.deviceCapabilities.armPush()")
    if deliver_push(page, origin=origin, data="synthetic"):
        shown = evaluate_js(
            page,
            "window.deviceCapabilities.pushSeen"
            ".then(() => window.deviceCapabilities.notifications())",
        )
        generic = [
            item
            for item in shown
            if item["tag"] == "device-capabilities"
            and item["body"] == "You have a new notification."
        ]
        checks.append(
            _sub(
                "handler_shows_generic_notification",
                *_verdict(
                    ok=len(generic) == 1,
                    detail="push event handled; fixed text, no payload content",
                ),
            )
        )
    else:
        checks.append(
            _sub(
                "handler_shows_generic_notification",
                "unmeasurable",
                f"DevTools push delivery exists only on Chromium, not {engine}",
            )
        )
    checks.append(
        _sub(
            "delivery_to_locked_device",
            "unmeasurable",
            "needs FCM/APNs and a real device; EG-14 RD-6",
        )
    )
    return checks


def _camera(page: Page, *, engine: str) -> list[Sub]:
    seen = _run(page, "camera")
    width = int(_num(seen, "width"))
    height = int(_num(seen, "height"))
    decoded = width > 0 and _num(seen, "luma_mean") > 0
    checks = [
        _sub(
            "capture_frame",
            *_verdict(
                ok=decoded,
                detail=f"{width}x{height} non-black frame drawn; "
                f"error={seen.get('error')}",
            ),
        ),
        _sub(
            "file_capture_fallback",
            *_verdict(
                ok=bool(seen.get("file_capture_fallback")),
                detail='input type=file capture="environment"',
            ),
        ),
    ]
    if engine == "chromium":
        checks.append(
            _sub(
                "frame_detail",
                *_verdict(
                    ok=_num(seen, "edge_energy") > 0,
                    detail="fake-device test pattern has edges; not optical sharpness",
                ),
            )
        )
        checks.append(
            _sub(
                "resolution_1280x720",
                *_verdict(
                    ok=width >= MIN_WIDTH and height >= MIN_HEIGHT,
                    detail=f"fake device delivered {width}x{height}",
                ),
            )
        )
    else:
        checks.append(
            _sub(
                "frame_detail",
                "unmeasurable",
                f"{engine} source is the harness's flat synthetic canvas, not a camera",
            )
        )
        checks.append(
            _sub(
                "resolution_1280x720",
                "unmeasurable",
                f"{engine} source is the harness's synthetic canvas, not a camera",
            )
        )
    checks.append(
        _sub(
            "document_legibility",
            "unmeasurable",
            "optical quality of a printed document needs a real camera; EG-14 RD-6",
        )
    )
    return checks


def _webauthn(page: Page, *, engine: str) -> list[Sub]:
    cdp = None
    if engine == "chromium":
        cdp, _authenticator = virtual_authenticator(page)
    try:
        seen = _run(page, "webauthn")
    finally:
        if cdp is not None:
            cdp.detach()
    checks = [
        _sub(
            "api_surface",
            *_verdict(
                ok=bool(seen.get("public_key_credential")), detail="PublicKeyCredential"
            ),
        )
    ]
    if engine == "chromium":
        checks.append(
            _sub(
                "platform_authenticator_available",
                *_verdict(
                    ok=bool(seen.get("platform_available")),
                    detail="virtual internal CTAP2 authenticator",
                ),
            )
        )
        checks.append(
            _sub(
                "platform_ceremony",
                *_verdict(
                    ok=bool(
                        seen.get("created")
                        and seen.get("attachment") == "platform"
                        and seen.get("asserted")
                        and seen.get("user_verified")
                        and seen.get("user_present")
                    ),
                    detail="create+get, platform attachment, UV and UP; "
                    f"error={seen.get('error')}",
                ),
            )
        )
    else:
        checks.append(
            _sub(
                "platform_authenticator_available",
                "unmeasurable",
                f"Playwright cannot attach an authenticator to {engine};"
                f" isUVPAA={seen.get('platform_available')}",
            )
        )
        checks.append(
            _sub(
                "platform_ceremony",
                "unmeasurable",
                "no virtual authenticator outside Chromium; EG-14 RD-6",
            )
        )
    return checks


def test_device_capability_study_emulation(
    renewal_engine: str, renewal_artifact_root: Path
) -> None:
    root = renewal_artifact_root / "device-capabilities"
    root.mkdir(mode=0o700, exist_ok=True)
    with _serve() as origin, sync_playwright() as driver:
        browser = launch_selected(driver, media=True)
        context = new_context(
            browser, viewport={"width": 412, "height": 839}, locale="pt-BR"
        )
        install_media(context, origin, granted=True)
        page = context.new_page()
        page.set_default_timeout(30_000)
        goto_settled(page, f"{origin}/index.html")
        grades = {
            "background-audio": _audio(page),
            "push": _push(page, origin, engine=renewal_engine),
            "camera-capture": _camera(page, engine=renewal_engine),
            "webauthn-platform": _webauthn(page, engine=renewal_engine),
        }
        page.screenshot(path=str(root / f"probes-{renewal_engine}.png"))
        (root / f"probes-{renewal_engine}.png").chmod(0o600)
        source = media_source(context)
        version = browser.version
        context.close()
        browser.close()
    report = {
        "schema_version": 1,
        "suite": "device-capabilities",
        "engine": renewal_engine,
        "engine_version": version,
        "evidence_level": EVIDENCE_LEVEL,
        "device_proof": DEVICE_PROOF,
        "profile": "412x839 viewport; touch and OS behaviour not emulated",
        "media_source": source,
        "criteria": [
            {
                "id": name,
                "status": criterion_status(grades[name]),
                "subchecks": grades[name],
            }
            for name in CRITERIA
        ],
    }
    validate_engine_report(report)
    destination = root / f"device-{renewal_engine}.json"
    destination.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    destination.chmod(0o600)
    # The harness itself must have measured something on every criterion.
    for name, subchecks in grades.items():
        assert any(item["result"] == "pass" for item in subchecks), name
