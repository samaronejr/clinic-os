"""Real-browser workspace shell: role navigation, clinic context, install, offline.

Owner access is confined to synthetic staff setup and one role revocation. The
served product runs as ``clinic_app``; the runner removes the database after
the suite. Every wait subscribes to a navigation or response, never a timer.
"""

from __future__ import annotations

import json
import os
import secrets
from contextlib import contextmanager, nullcontext
from functools import partialmethod
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
import pytest
from django.contrib.auth.hashers import make_password
from django.utils.translation import gettext
from django_otp.oath import TOTP
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, expect

from renewal.browser._navigation import goto_refused
from renewal.browser._page_wait import await_autofocus, evaluate_js, wait_for_js
from renewal.browser.engines import (
    assert_only_refused_document_logged,
    full_page_screenshot,
    navigations_are_worker_controlled,
    offline_navigation_error,
    worker_answers_offline,
)
from renewal.browser.test_retention import (
    POINTER_WATCH_JS,
    _first_line,
    _StepRecorder,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from playwright.sync_api import Browser, BrowserContext, Page

PATIENT = "Helena Sintética da Silva"
CLINIC_A = "Clínica Vila Mariana"
CLINIC_B = "Clínica Vila Olímpia"  # sorts after clinic A, so A is the default
WIDTHS = (1280, 768, 375, 320)
OK = 200
NOT_FOUND = 404
FORBIDDEN = 403


class _FixtureSecret(str):
    """Preserve credential values for clients, but redact fixture failure reprs."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "<fixture secret>"


@pytest.fixture
def workspace_staff(renewal_base_url: str) -> dict[str, str]:
    """Seed a two-clinic receptionist, a TOTP-enrolled physician and clinic B."""
    del renewal_base_url  # The runner fixture rejects use outside its lifecycle.
    values = {
        "clinic_a": os.environ["CLINIC_RENEWAL_CLINIC_ID"],
        "clinic_b": str(uuid4()),
        "organization": os.environ["CLINIC_RENEWAL_ORGANIZATION_ID"],
        "receptionist": f"recepcao-{uuid4().hex[:8]}",
        "receptionist_id": str(uuid4()),
        "physician": f"medico-{uuid4().hex[:8]}",
        "physician_id": str(uuid4()),
        "password": _FixtureSecret(secrets.token_urlsafe(24)),
        "totp_key": _FixtureSecret(secrets.token_hex(20)),
    }
    with psycopg.connect(
        os.environ["CLINIC_RENEWAL_FIXTURE_DATABASE_URL"]
    ) as connection:
        connection.execute(
            "SELECT set_config('app.current_tenant', %s, true)",
            [values["organization"]],
        )
        connection.execute(
            "UPDATE clinic_app.identity_clinic SET name = %s WHERE id = %s",
            [CLINIC_A, values["clinic_a"]],
        )
        connection.execute(
            "INSERT INTO clinic_app.identity_clinic "
            "(id, organization_id, name, crm_uf, timezone) "
            "VALUES (%s, %s, %s, 'SP', 'America/Sao_Paulo')",
            [values["clinic_b"], values["organization"], CLINIC_B],
        )
        for user_id, username in (
            (values["receptionist_id"], values["receptionist"]),
            (values["physician_id"], values["physician"]),
        ):
            connection.execute(
                "INSERT INTO clinic_app.identity_user "
                "(id, username, password, email, first_name, last_name, is_active, "
                "is_staff, is_superuser, date_joined) "
                "VALUES (%s, %s, %s, %s, '', '', true, false, false, now())",
                [
                    user_id,
                    username,
                    make_password(values["password"]),
                    f"{username}@workspace.invalid",
                ],
            )
        for user_id, clinic_id, role in (
            (values["receptionist_id"], values["clinic_a"], "receptionist"),
            (values["receptionist_id"], values["clinic_b"], "receptionist"),
            (values["physician_id"], values["clinic_a"], "physician"),
        ):
            connection.execute(
                "INSERT INTO clinic_app.identity_userclinicrole "
                "(id, user_id, organization_id, clinic_id, role) "
                "VALUES (%s, %s, %s, %s, %s)",
                [str(uuid4()), user_id, values["organization"], clinic_id, role],
            )
        connection.execute("SET ROLE clinic_app")
        connection.execute(
            "SELECT set_config('app.current_user_id', %s, true)",
            [values["physician_id"]],
        )
        connection.execute(
            "INSERT INTO clinic_app.otp_totp_totpdevice "
            "(name, confirmed, key, step, t0, digits, tolerance, drift, last_t, "
            "user_id, throttling_failure_count, created_at) "
            "VALUES ('Clinic OS authenticator', true, %s, 30, 0, 6, 1, 0, -1, "
            "%s, 0, now())",
            [values["totp_key"], values["physician_id"]],
        )
    return values


@pytest.fixture
def workspace_browser(renewal_page: Page) -> Browser:
    browser = renewal_page.context.browser
    assert browser is not None
    return browser


@pytest.fixture
def desktop(workspace_browser: Browser) -> Iterator[Page]:
    context = workspace_browser.new_context(
        locale="pt-BR", viewport={"width": 1280, "height": 800}
    )
    page = context.new_page()
    page.set_default_timeout(20_000)
    yield page
    context.close()


def _watch_errors(page: Page) -> list[str]:
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on(
        "console",
        lambda message: (
            errors.append(message.text) if message.type == "error" else None
        ),
    )
    return errors


def _capture(page: Page, root: Path, name: str) -> str:
    destination = root / "workspace" / f"{name}.png"
    destination.parent.mkdir(mode=0o700, exist_ok=True)
    return ", ".join(path.name for path in full_page_screenshot(page, destination))


# Page state around a workspace submit, for the hosted WebKit stall where the
# sign-in click returned and no POST reached the server (fix-a7). Structure
# only: paths, states and validity flags, never field values or headers.
SUBMIT_STATE_JS = """(selector) => {
  const sw = navigator.serviceWorker;
  const button = document.querySelector(selector);
  let target = null;
  if (button) {
    const r = button.getBoundingClientRect();
    const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    target = {
      box: [r.left, r.top, r.width, r.height],
      hit: hit === button ? 'button'
        : hit ? hit.tagName.toLowerCase() + (hit.id ? '#' + hit.id : '') : null,
      disabled: button.disabled,
    };
  }
  const form = button && button.form;
  return {
    path: location.pathname,
    readyState: document.readyState,
    visibility: document.visibilityState,
    hasFocus: document.hasFocus(),
    active: document.activeElement
      ? document.activeElement.tagName.toLowerCase()
        + (document.activeElement.id ? '#' + document.activeElement.id : '')
      : null,
    htmx: typeof htmx,
    serviceWorker: sw ? {
      controlled: Boolean(sw.controller),
      controller: sw.controller ? sw.controller.state : null,
      registration: window.__disposalRegistration || null,
    } : null,
    button: target,
    pointer: window.__disposalPointer || null,
    fields: window.__submitFields || null,
    busy: form ? form.getAttribute('aria-busy') : null,
    form: form ? Array.from(form.elements).filter((e) => e.name).map((e) => ({
      name: e.name, type: e.type, filled: e.value !== '', valid: e.validity.valid,
    })) : null,
  };
}"""


# Which control each focus change and text input reached, in order: target
# tag#id and input type only, never the inserted text.
FIELD_WATCH_JS = """() => {
  const seen = [];
  window.__submitFields = seen;
  const name = (t) => (t && t.tagName
    ? t.tagName.toLowerCase() + (t.id ? '#' + t.id : '') : null);
  for (const type of ['focusin', 'input']) {
    document.addEventListener(type, (event) => {
      seen.push({type, target: name(event.target),
        inputType: event.inputType || null, t: Math.round(performance.now())});
    }, {capture: true});
  }
  return true;
}"""


def _submit_state(page: Page, selector: str) -> object:
    """Read the submit's page state under wait_for_js's driver-side deadline."""
    return wait_for_js(page, SUBMIT_STATE_JS, arg=selector).json_value()


@contextmanager
def _submit_diagnostics(page: Page, selector: str) -> Iterator[None]:
    """Record one submit's events and page state; write them only on failure.

    The report lands in ``<artifact root>/workspace/`` (0o600) under the
    failing test's name, and the step's own error propagates unchanged.
    """
    recorder = _StepRecorder(page)
    recorder.attach()
    try:
        wait_for_js(page, POINTER_WATCH_JS)
        before = _submit_state(page, selector)
        try:
            yield
        except BaseException as error:
            try:
                after = _submit_state(page, selector)
            except PlaywrightError as problem:
                # A closed or crashed page must not mask the step's own error.
                after = {"unavailable": _first_line(problem)}
            test = os.environ["PYTEST_CURRENT_TEST"].split("::")[-1].split(" ")[0]
            folder = Path(os.environ["CLINIC_RENEWAL_ARTIFACT_ROOT"]) / "workspace"
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            destination = folder / f"submit-diagnostics-{test}.json"
            report = {
                "error": _first_line(error),
                "selector": selector,
                "elapsed": recorder.elapsed(),
                "before": before,
                "after": after,
                "events": recorder.events,
            }
            destination.write_text(json.dumps(report, indent=2) + "\n")
            destination.chmod(0o600)
            raise
    finally:
        recorder.detach()


def _submit(page: Page, selector: str, *, navigation: bool = True) -> None:
    """Finish the triggered document, not merely its POST's response headers."""
    with (
        _submit_diagnostics(page, selector),
        page.expect_navigation(wait_until="load") if navigation else nullcontext(),
        page.expect_response(
            lambda response: response.request.method == "POST"
        ) as received,
    ):
        page.locator(selector).click()
    assert received.value.status in {200, 204, 302, 303}


def _sign_in(page: Page, base_url: str, username: str, password: str) -> None:
    page.goto(f"{base_url}/auth/login/")
    wait_for_js(page, FIELD_WATCH_JS)
    expect(page.locator(".nav-list")).to_have_count(0)  # authentication stays focused
    expect(page.locator(".nav-brand .nav-wordmark")).to_have_text("Clinic Ops")
    await_autofocus(page.locator("#id_username"))
    page.locator("#id_username").fill(username)
    page.locator("#id_password").fill(password)
    _submit(page, "button[type=submit]")


def _register_and_find_patient(page: Page, base_url: str, patients: str) -> None:
    page.goto(f"{base_url}{patients}new/")
    page.locator("#id_full_name").fill(PATIENT)
    page.locator("#id_birth_date").fill("1990-05-17")
    _submit(page, "button[type=submit]")
    page.wait_for_url(f"**{patients}")
    page.locator("#id_q").fill(PATIENT)
    _submit(page, "#patient-search-form button[type=submit]", navigation=False)
    expect(page.locator(".intake-table tbody")).to_contain_text(PATIENT)


def test_submit_finishes_native_and_htmx_redirect_navigation(
    desktop: Page,
    renewal_base_url: str,
    workspace_staff: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An incidental click wait must not be what makes the helper safe.
    monkeypatch.setattr(
        Locator, "click", partialmethod(Locator.click, no_wait_after=True)
    )
    loaded: list[str] = []
    desktop.on("load", lambda page: loaded.append(page.url))
    original = _submit

    def checked(page: Page, selector: str, *, navigation: bool = True) -> None:
        before = len(loaded)
        original(page, selector, navigation=navigation)
        if navigation:
            assert len(loaded) > before

    monkeypatch.setattr(f"{__name__}._submit", checked)
    assert "dsn" not in workspace_staff
    for key in ("password", "totp_key"):
        assert workspace_staff[key] not in repr(workspace_staff)
    _sign_in(
        desktop,
        renewal_base_url,
        workspace_staff["receptionist"],
        workspace_staff["password"],
    )
    _register_and_find_patient(
        desktop,
        renewal_base_url,
        f"/intake/clinics/{workspace_staff['clinic_a']}/patients/",
    )


def _modules(page: Page) -> list[str]:
    modules = page.locator(".nav-list a[data-module]").evaluate_all(
        "links => links.map(link => link.dataset.module)"
    )
    return [str(module) for module in modules]


def _no_overflow(page: Page) -> bool:
    return bool(evaluate_js(page, "document.documentElement.scrollWidth <= innerWidth"))


def _await_worker(page: Page) -> None:
    """Observe worker control directly, with a bounded failure timeout."""
    page.evaluate(
        """() => new Promise((resolve, reject) => {
          const workers = navigator.serviceWorker;
          if (workers.controller) { resolve(); return; }
          const claimed = () => {
            if (!workers.controller) return;
            clearTimeout(timeout);
            workers.removeEventListener('controllerchange', claimed);
            resolve();
          };
          workers.addEventListener('controllerchange', claimed);
          const timeout = setTimeout(() => {
            workers.removeEventListener('controllerchange', claimed);
            reject(new Error('Worker did not claim the page'));
          }, 20000);
        })"""
    )


CACHED_FOR_ANONYMOUS_JS = """async (path) => {
  const request = new Request(path, {credentials: 'omit'});
  for (const name of await caches.keys()) {
    if (await (await caches.open(name)).match(request)) return true;
  }
  return false;
}"""


def _cached_urls(page: Page) -> list[str]:
    urls = page.evaluate(
        """async () => {
          const names = await caches.keys();
          const urls = [];
          for (const name of names) {
            const cache = await caches.open(name);
            for (const request of await cache.keys()) {
              urls.push(new URL(request.url).pathname);
            }
          }
          return urls;
        }"""
    )
    return [str(url) for url in urls]


def _sign_in_receptionist(page: Page, base_url: str, staff: dict[str, str]) -> None:
    _sign_in(page, base_url, staff["receptionist"], staff["password"])
    page.wait_for_url("**/auth/protected/")


def test_receptionist_reaches_every_module_and_switches_clinic(
    desktop: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    workspace_staff: dict[str, str],
    browser_report: dict[str, object],
) -> None:
    page = desktop
    errors = _watch_errors(page)
    root = renewal_artifact_root
    agenda_a = f"/scheduling/clinics/{workspace_staff['clinic_a']}/agenda/"
    patients_a = f"/intake/clinics/{workspace_staff['clinic_a']}/patients/"

    _sign_in_receptionist(page, renewal_base_url, workspace_staff)
    expect(page.locator(".nav-clinic")).to_contain_text(CLINIC_A)
    assert _modules(page) == ["agenda", "patients", "finance", "operations"]
    _capture(page, root, "receptionist-landing-1280")

    # Brand link and Agenda entry both land on today's agenda of this clinic.
    with page.expect_navigation():
        page.locator(".nav-brand").click()
    assert page.url.endswith(agenda_a)
    expect(page.locator("a[data-module=agenda]")).to_have_attribute(
        "aria-current", "page"
    )
    expect(page.locator("a[data-module=agenda] .nav-badge")).to_contain_text("hoje")
    _capture(page, root, "receptionist-agenda-1280")

    # Register and find one synthetic patient through the real module.
    with page.expect_navigation():
        page.locator("a[data-module=patients]").click()
    expect(page.locator("a[data-module=patients]")).to_have_attribute(
        "aria-current", "page"
    )
    _register_and_find_patient(page, renewal_base_url, patients_a)
    _capture(page, root, "receptionist-patients-1280")

    # Clinic switcher: native disclosure, keyboard reachable, changes context.
    page.locator(".nav-switch-summary").focus()
    page.keyboard.press("Enter")
    expect(page.locator(".nav-switch")).to_have_attribute("open", "")
    _capture(page, root, "receptionist-switch-open-1280")
    with page.expect_navigation():
        page.locator(".nav-switch-list a", has_text=CLINIC_B).click()
    assert workspace_staff["clinic_b"] in page.url
    expect(page.locator(".nav-clinic")).to_contain_text(CLINIC_B)
    expect(page.locator(".nav-switch-list")).to_contain_text(CLINIC_A)

    # The remembered clinic survives a non-clinic page.
    page.goto(f"{renewal_base_url}/auth/protected/")
    expect(page.locator(".nav-clinic")).to_contain_text(CLINIC_B)
    assert not errors
    checks = browser_report["checks"]
    assert isinstance(checks, list)
    checks.append(
        {
            "surface": "workspace",
            "assertion": "receptionist modules, agenda entry, clinic switcher",
            "modules": ["agenda", "patients", "finance", "operations"],
            "console_errors": errors,
        }
    )


def test_keyboard_order_and_reflow_hold_at_every_width(
    desktop: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    workspace_staff: dict[str, str],
) -> None:
    page = desktop
    root = renewal_artifact_root
    agenda_a = f"/scheduling/clinics/{workspace_staff['clinic_a']}/agenda/"
    _sign_in_receptionist(page, renewal_base_url, workspace_staff)

    # Keyboard order: skip link, brand, clinic switcher, search, destinations,
    # sign out, then the Agenda section row (Agenda, Availability).
    page.goto(f"{renewal_base_url}{agenda_a}")
    order: list[str] = []
    for _ in range(11):
        page.keyboard.press("Tab")
        order.append(
            page.evaluate(
                "document.activeElement.dataset.module"
                " || document.activeElement.className"
            )
        )
    assert order == [
        "skip-link",
        "nav-brand",
        "nav-switch-summary",
        "nav-command",
        "agenda",
        "patients",
        "finance",
        "operations",
        "nav-link",
        "tab",
        "tab",
    ]
    assert (
        page.evaluate("getComputedStyle(document.activeElement).outlineStyle") != "none"
    )

    # Reflow: no horizontal scrolling at any width, including 320px.
    for width in WIDTHS:
        page.set_viewport_size({"width": width, "height": 900})
        assert _no_overflow(page), width
        _capture(page, root, f"receptionist-agenda-{width}")
    page.set_viewport_size({"width": 320, "height": 900})
    with page.expect_navigation():
        page.locator(".shell-subnav a[data-tab=availability]").click()
    assert "/availability/" in page.url
    assert _no_overflow(page)
    _capture(page, root, "receptionist-availability-320")


def test_installation_metadata_and_static_only_worker_resolve(
    desktop: Page,
    renewal_base_url: str,
    workspace_staff: dict[str, str],
    browser_report: dict[str, object],
) -> None:
    page = desktop
    errors = _watch_errors(page)
    agenda_a = f"/scheduling/clinics/{workspace_staff['clinic_a']}/agenda/"
    _sign_in_receptionist(page, renewal_base_url, workspace_staff)

    manifest = page.request.get(f"{renewal_base_url}/static/manifest.webmanifest")
    assert manifest.status == OK
    assert manifest.headers["content-type"].startswith("application/manifest+json")
    manifest_body = manifest.json()
    assert manifest_body["name"] == "Clinic Ops"
    assert manifest_body["display"] == "standalone"
    for icon in manifest_body["icons"]:
        icon_response = page.request.get(f"{renewal_base_url}{icon['src']}")
        assert icon_response.status == OK, icon["src"]
        assert icon_response.headers["content-type"].startswith(icon["type"])
    assert page.locator('link[rel="manifest"]').count() == 1

    # The worker controls the root scope and holds versioned static bytes only.
    if not navigations_are_worker_controlled(page.context):
        # Firefox under Playwright controls only the page the worker claimed
        # (engines.navigations_are_worker_controlled): the signed-in landing.
        _await_worker(page)
    page.goto(f"{renewal_base_url}{agenda_a}")
    if navigations_are_worker_controlled(page.context):
        _await_worker(page)
    scope = page.evaluate("navigator.serviceWorker.ready.then(r => r.scope)")
    assert scope == f"{renewal_base_url}/"
    cached = _cached_urls(page)
    assert cached
    assert all(url.startswith("/static/") for url in cached)
    assert "/static/css/clinic-os.css" in cached
    worker = page.request.get(f"{renewal_base_url}/sw.js")
    assert worker.status == OK
    assert "no-store" in worker.headers["cache-control"]
    assert not errors
    checks = browser_report["checks"]
    assert isinstance(checks, list)
    checks.append(
        {
            "surface": "workspace",
            "assertion": "manifest, icons and root-scope static-only worker",
            "cached_paths": sorted(set(cached)),
            "console_errors": errors,
        }
    )


def test_worker_never_stores_credential_bearing_requests(
    desktop: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    workspace_staff: dict[str, str],
) -> None:
    page = desktop
    asset = "/static/css/clinic-os.css"
    legacy_cache = "clinic-os-static-legacy-credential-probe"
    page.goto(f"{renewal_base_url}/auth/login/")
    page.evaluate(
        """async ({name, asset}) => {
          const cache = await caches.open(name);
          await cache.put(new Request(asset, {
            headers: {Authorization: 'Bearer LEGACY_SYNTHETIC_CREDENTIAL'}
          }), new Response('legacy static fixture'));
        }""",
        {"name": legacy_cache, "asset": asset},
    )
    _sign_in_receptionist(page, renewal_base_url, workspace_staff)
    _await_worker(page)
    assert legacy_cache not in page.evaluate("caches.keys()")

    # Remove the allowlisted asset first: a cache hit must not hide a bad write.
    page.evaluate(
        """async url => {
          for (const name of await caches.keys()) {
            await (await caches.open(name)).delete(url);
          }
        }""",
        asset,
    )
    cases: list[tuple[str, dict[str, object]]] = [
        (
            asset,
            {"credentials": "omit", "headers": {"Authorization": "Bearer SYNTHETIC"}},
        ),
        (
            asset,
            {
                "credentials": "include",
                "headers": {"Authorization": "Bearer SYNTHETIC"},
            },
        ),
        (asset, {"credentials": "include"}),
        (asset, {"credentials": "same-origin"}),
        (asset, {"credentials": "omit", "method": "POST"}),
        (asset, {"credentials": "omit", "method": "HEAD"}),
        (
            asset + "?gate-credential-probe=1",
            {
                "credentials": "include",
                "headers": {"Authorization": "Bearer SYNTHETIC"},
            },
        ),
        (asset + "?unversioned=1", {"credentials": "omit"}),
        ("/static/brand/clinic-ops-icon-192.png", {"credentials": "omit"}),
    ]
    checks: list[dict[str, object]] = []
    for path, options in cases:
        with page.expect_response(renewal_base_url + path) as received:
            status = page.evaluate(
                """async ({path, options}) => {
                  const response = await fetch(path, {...options, cache: 'no-store'});
                  await response.arrayBuffer();
                  return response.status;
                }""",
                {"path": path, "options": options},
            )
        assert not received.value.from_service_worker
        if options["credentials"] != "omit":
            assert "sessionid=" in received.value.request.all_headers()["cookie"]
        assert page.evaluate(
            "url => caches.match(url).then(hit => hit === undefined)", path
        )
        checks.append(
            {"path": path, "options": options, "status": status, "stored": False}
        )

    # Positive control: an anonymous allowlisted fetch still fills the cache.
    with page.expect_response(renewal_base_url + asset) as received:
        assert (
            page.evaluate(
                """url => fetch(url, {credentials: 'omit', cache: 'no-store',
              headers: {'X-Cache-Probe': 'not-persisted'}}).then(r => r.status)""",
                asset,
            )
            == OK
        )
    assert received.value.from_service_worker
    stored = page.evaluate(
        """async () => {
          const stored = [];
          for (const name of await caches.keys()) {
            for (const request of await (await caches.open(name)).keys()) {
              stored.push({cache: name, path: new URL(request.url).pathname,
                method: request.method, credentials: request.credentials,
                headers: [...request.headers]});
            }
          }
          return stored;
        }"""
    )
    assert any(entry["path"] == asset for entry in stored)
    assert all(entry["method"] == "GET" for entry in stored)
    assert all(entry["credentials"] == "omit" for entry in stored)
    assert all(entry["headers"] == [] for entry in stored)
    destination = renewal_artifact_root / "workspace" / "credential-cache-report.json"
    destination.parent.mkdir(exist_ok=True)
    destination.write_text(
        json.dumps(
            {"legacy_cache_removed": True, "probes": checks, "stored": stored}, indent=2
        )
        + "\n"
    )


def test_physician_sees_only_clinical_modules_and_registry_stays_denied(
    desktop: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    workspace_staff: dict[str, str],
) -> None:
    page = desktop
    errors = _watch_errors(page)
    patients_a = f"/intake/clinics/{workspace_staff['clinic_a']}/patients/"
    agenda_b = f"/scheduling/clinics/{workspace_staff['clinic_b']}/agenda/"

    _sign_in(
        page,
        renewal_base_url,
        workspace_staff["physician"],
        workspace_staff["password"],
    )
    page.wait_for_url("**/auth/verify/**")
    expect(page.locator(".nav-list")).to_have_count(0)
    token = TOTP(bytes.fromhex(workspace_staff["totp_key"]), 30, 0, 6, 0).token()
    page.locator("#id_otp_token").fill(f"{token:06d}")
    _submit(page, "button[type=submit]")
    page.wait_for_url("**/auth/protected/")
    assert _modules(page) == ["agenda", "patients", "operations"]
    expect(page.locator(".nav-clinic")).to_contain_text(CLINIC_A)
    expect(page.locator(".nav-switch")).to_have_count(0)
    _capture(page, renewal_artifact_root, "physician-landing-1280")

    # Forbidden role: the registry is refused on GET and on POST, never offered.
    registry = page.goto(f"{renewal_base_url}{patients_a}")
    assert registry is not None
    assert registry.status == NOT_FOUND
    assert _modules(page) == ["agenda", "patients", "operations"]
    expect(page.locator("#id_q")).to_have_count(0)
    csrf = next(
        cookie["value"]
        for cookie in page.context.cookies()
        if cookie["name"] == "csrftoken"
    )
    refused = page.request.post(
        f"{renewal_base_url}{patients_a}",
        form={"csrfmiddlewaretoken": csrf, "q": PATIENT, "page": "1"},
    )
    assert refused.status == NOT_FOUND
    assert PATIENT not in refused.text()
    assert PATIENT not in page.content()
    _capture(page, renewal_artifact_root, "physician-registry-denied-1280")

    # Forbidden clinic: denied, and the shell keeps the physician's own clinic.
    response = page.goto(f"{renewal_base_url}{agenda_b}")
    assert response is not None
    assert response.status == NOT_FOUND
    expect(page.locator("h1")).to_have_text(gettext("Page unavailable"))
    expect(page.locator(".nav-clinic")).to_contain_text(CLINIC_A)
    assert CLINIC_B not in page.content()
    page.set_viewport_size({"width": 375, "height": 900})
    assert _no_overflow(page)
    _capture(page, renewal_artifact_root, "physician-foreign-clinic-denied-375")
    # The only console entries are the two refused documents themselves
    # (where the engine logs failed responses at all).
    assert_only_refused_document_logged(page, errors, "404", documents=2)


def test_stale_clinic_context_is_dropped_after_revocation(
    desktop: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    workspace_staff: dict[str, str],
) -> None:
    page = desktop
    agenda_b = f"/scheduling/clinics/{workspace_staff['clinic_b']}/agenda/"
    _sign_in(
        page,
        renewal_base_url,
        workspace_staff["receptionist"],
        workspace_staff["password"],
    )
    page.wait_for_url("**/auth/protected/")
    page.goto(f"{renewal_base_url}{agenda_b}")
    expect(page.locator(".nav-clinic")).to_contain_text(CLINIC_B)

    with psycopg.connect(
        os.environ["CLINIC_RENEWAL_FIXTURE_DATABASE_URL"]
    ) as connection:
        connection.execute(
            "SELECT set_config('app.current_tenant', %s, true)",
            [workspace_staff["organization"]],
        )
        connection.execute(
            "DELETE FROM clinic_app.identity_userclinicrole "
            "WHERE user_id = %s AND clinic_id = %s",
            [workspace_staff["receptionist_id"], workspace_staff["clinic_b"]],
        )

    page.goto(f"{renewal_base_url}/auth/protected/")
    expect(page.locator(".nav-clinic")).to_contain_text(CLINIC_A)
    assert CLINIC_B not in page.content()
    expect(page.locator(".nav-switch")).to_have_count(0)
    _capture(page, renewal_artifact_root, "stale-clinic-replaced-1280")
    response = page.goto(f"{renewal_base_url}{agenda_b}")
    assert response is not None
    assert response.status == NOT_FOUND
    assert CLINIC_B not in page.content()
    _capture(page, renewal_artifact_root, "stale-clinic-denied-1280")


def _settle_offline_navigation(page: Page) -> Page:
    """Close the page holding the refused navigation; continue in a fresh one.

    Firefox's network-error document retries its navigation when the browser
    comes back online, and that retry can start after a later ``goto`` and
    interrupt it. Chromium commits its error document after ``goto`` has
    already raised, so even an ``about:blank`` navigation can be overtaken.
    Closing the page, while still offline, ends every navigation and handler
    it owns in every engine; the recovery ``goto`` cannot overlap them.
    """
    context = page.context
    page.close()
    fresh = context.new_page()
    fresh.set_default_timeout(20_000)  # the suite's page default
    return fresh


def test_offline_settle_discards_a_retrying_document(
    desktop: Page, renewal_base_url: str
) -> None:
    page = desktop
    login = f"{renewal_base_url}/auth/login/"
    page.goto(login)
    # Simulate the engine's pending retry: a reconnect listener owned by the
    # refused document that would start a navigation the test did not request.
    evaluate_js(
        page,
        """url => {
          window.__retryPending = true;
          addEventListener('online', () => location.assign(url));
          return true;
        }""",
        login,
    )
    page.context.set_offline(offline=True)
    try:
        page = _settle_offline_navigation(page)
        assert evaluate_js(page, "() => window.__retryPending === undefined")
    finally:
        page.context.set_offline(offline=False)
    response = page.goto(login)
    assert response is not None
    assert response.status == OK


def test_offline_reload_reveals_no_patient_content(
    workspace_browser: Browser,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    workspace_staff: dict[str, str],
) -> None:
    context: BrowserContext = workspace_browser.new_context(
        locale="pt-BR", viewport={"width": 1280, "height": 800}
    )
    page = context.new_page()
    page.set_default_timeout(20_000)
    patients_a = f"/intake/clinics/{workspace_staff['clinic_a']}/patients/"
    fetch_status = (
        "url => fetch(url, {cache: 'no-store', credentials: 'omit'})"
        ".then(r => r.status).catch(e => 'rejected: ' + e.message)"
    )
    try:
        _sign_in(
            page,
            renewal_base_url,
            workspace_staff["receptionist"],
            workspace_staff["password"],
        )
        page.wait_for_url("**/auth/protected/")
        _await_worker(page)
        _register_and_find_patient(page, renewal_base_url, patients_a)
        cached_before = _cached_urls(page)
        assert cached_before
        assert all(url.startswith("/static/") for url in cached_before)
        assert page.evaluate(
            "caches.match(location.href).then(hit => hit === undefined)"
        )

        # Offline on the controlled page: static bytes answer from the worker
        # cache for anonymous requests only (HTTP cache bypassed); documents,
        # product routes and cookie-capable requests do not.
        context.set_offline(offline=True)
        offline = {
            "stylesheet": page.evaluate(fetch_status, "/static/css/clinic-os.css"),
            "credentialed_stylesheet": page.evaluate(
                """() => fetch('/static/css/clinic-os.css', {
                  cache: 'no-store', credentials: 'include'
                }).then(r => r.status).catch(e => 'rejected: ' + e.message)"""
            ),
            "patients": page.evaluate(fetch_status, patients_a),
            "worker": page.evaluate(fetch_status, "/sw.js"),
        }
        if worker_answers_offline(context):
            assert offline["stylesheet"] == OK
        else:
            # The engine cannot let the worker answer offline
            # (engines.worker_answers_offline); its cache must still hold the
            # stylesheet for exactly the anonymous request it would answer.
            assert page.evaluate(CACHED_FOR_ANONYMOUS_JS, "/static/css/clinic-os.css")
        assert str(offline["credentialed_stylesheet"]).startswith("rejected")
        assert str(offline["patients"]).startswith("rejected")
        assert str(offline["worker"]).startswith("rejected")
        # The refused navigation leaves an error document committing under
        # the refused URL; goto_refused drains that commit (fix-a14), and the
        # page that held it is then closed while still offline, so neither
        # the commit nor Firefox's reconnect retry can reach the recovery.
        with pytest.raises(PlaywrightError, match=offline_navigation_error(context)):
            goto_refused(page, f"{renewal_base_url}{patients_a}")
        page = _settle_offline_navigation(page)
        context.set_offline(offline=False)

        # The recovery runs in the fresh page, which never held the error
        # document.
        page.goto(f"{renewal_base_url}{patients_a}")
        cached_after = _cached_urls(page)
        assert all(url.startswith("/static/") for url in cached_after)
        bodies = page.evaluate(
            """async () => {
              const bodies = [];
              for (const name of await caches.keys()) {
                const cache = await caches.open(name);
                for (const request of await cache.keys()) {
                  const hit = await cache.match(request);
                  bodies.push(await hit.text());
                }
              }
              return bodies;
            }"""
        )
        assert bodies
        assert not any(PATIENT in body for body in bodies)
        _capture(page, renewal_artifact_root, "offline-recovered-1280")
        (renewal_artifact_root / "workspace" / "offline-report.json").write_text(
            json.dumps(
                {
                    "cached_paths_before": sorted(set(cached_before)),
                    "cached_paths_after": sorted(set(cached_after)),
                    "offline_fetch": offline,
                    "offline_navigation": offline_navigation_error(context),
                    "patient_in_any_cached_body": False,
                },
                indent=2,
            )
            + "\n"
        )
    finally:
        context.close()
