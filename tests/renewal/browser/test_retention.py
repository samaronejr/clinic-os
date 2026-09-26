"""Real clinic_app proof: policies, holds, releases and exports."""

from __future__ import annotations

import hashlib
import io
import json
import secrets
import shutil
import time
import zipfile
from contextlib import contextmanager
from typing import TYPE_CHECKING
from urllib.parse import urlsplit
from uuid import uuid4

import psycopg
import pytest
import rfc8785
from django.contrib.auth.hashers import make_password
from django_otp.oath import TOTP
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import expect

from renewal.browser._page_wait import click_when_hittable, wait_for_js
from renewal.browser.engines import failed_responses_logged, new_context
from renewal.browser.test_availability import (
    _sign_in,
    _sign_in_physician,
    _sign_in_receptionist,
    availability_staff,
)
from renewal.browser.test_encounter import DAYS, press, press_in_view, seed
from renewal.browser.test_patient_access import (
    MIN_TARGET_PX,
    ZOOM_FACTOR,
    ZOOM_WINDOW,
    _no_overflow,
    _overflowing,
    _redeem,
    _ring,
    _tab_until,
    _watch_errors,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path
    from typing import Any

    from playwright.sync_api import (
        Browser,
        BrowserContext,
        ConsoleMessage,
        Frame,
        Page,
        Request,
        Response,
    )

__all__ = ("availability_staff",)

FIRST = {
    "subjective": "Relato sintético retenção",
    "objective": "Exame sintético retenção",
    "assessment": "Avaliação sintética retenção",
    "plan": "Plano sintético retenção",
}


def capture(page: Page, root: Path, state: str, width: int) -> None:
    folder = root / "retention"
    folder.mkdir(exist_ok=True, mode=0o700)
    page.screenshot(path=str(folder / f"{state}-{width}.png"), full_page=True)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


# Page state at the check_disposal click, for the hosted Firefox stall where
# the click returned but no POST reached the server. Structure only: paths,
# states and validity flags, never field values, headers or bodies.
DISPOSAL_STATE_JS = """() => {
  const sw = navigator.serviceWorker;
  const state = (worker) => (worker ? worker.state : null);
  const button = document.querySelector(
    '#disposal-form button[value="check_disposal"]');
  let target = null;
  if (button) {
    const r = button.getBoundingClientRect();
    const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    target = {
      box: [r.left, r.top, r.width, r.height],
      hit: hit === button ? 'button'
        : hit ? hit.tagName.toLowerCase() + (hit.id ? '#' + hit.id : '') : null,
      disabled: button.disabled,
      connected: button.isConnected,
    };
  }
  const form = button && button.form;
  return {
    path: location.pathname,
    readyState: document.readyState,
    visibility: document.visibilityState,
    hasFocus: document.hasFocus(),
    active: document.activeElement
      ? document.activeElement.tagName.toLowerCase() : null,
    scroll: [scrollX, scrollY],
    viewport: [innerWidth, innerHeight],
    serviceWorker: sw ? {
      controlled: Boolean(sw.controller),
      controller: state(sw.controller),
      registration: window.__disposalRegistration || null,
    } : null,
    button: target,
    pointer: window.__disposalPointer || null,
    form: form ? Array.from(form.elements).filter((e) => e.name).map((e) => ({
      name: e.name, type: e.type, filled: e.value !== '', valid: e.validity.valid,
    })) : null,
  };
}"""
# Where the step's pointer and submit events actually land (capture phase):
# event type, trust, viewport point, scroll offset and target tag#id only.
POINTER_WATCH_JS = """() => {
  const seen = [];
  window.__disposalPointer = seen;
  // Never awaited: Firefox's getRegistration() can stay pending, and an
  // evaluate that awaits it would hang the suite (fix-a5 round 3). The state
  // snapshot reads whatever it resolved to so far.
  const sw = navigator.serviceWorker;
  if (sw) {
    const state = (worker) => (worker ? worker.state : null);
    window.__disposalRegistration = {settled: false};
    sw.getRegistration().then((reg) => {
      window.__disposalRegistration = {
        settled: true,
        installing: state(reg && reg.installing),
        waiting: state(reg && reg.waiting),
        active: state(reg && reg.active),
      };
    });
  }
  for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup',
      'click', 'submit']) {
    document.addEventListener(type, (event) => {
      const t = event.target;
      seen.push({
        type,
        trusted: event.isTrusted,
        point: 'clientX' in event ? [event.clientX, event.clientY] : null,
        scrollY: scrollY,
        target: t && t.tagName ? t.tagName.toLowerCase() + (t.id ? '#' + t.id : '')
          + (t.tagName === 'BUTTON' && t.value ? '[value=' + t.value + ']' : '') : null,
      });
    }, {capture: true});
  }
  return true;
}"""
CONSOLE_LIMIT = 200


class _StepRecorder:
    """Network, console and navigation events of one page step, PHI-free."""

    def __init__(self, page: Page) -> None:
        self.page = page
        self.start = time.monotonic()
        self.events: list[dict[str, object]] = []
        self.active = True

    def elapsed(self) -> float:
        return round(time.monotonic() - self.start, 3)

    def _note(self, kind: str, **fields: object) -> None:
        if self.active:
            self.events.append({"t": self.elapsed(), "kind": kind, **fields})

    def request(self, request: Request) -> None:
        self._note(
            "request",
            method=request.method,
            path=urlsplit(request.url).path,
            type=request.resource_type,
            navigation=request.is_navigation_request(),
        )

    def failed(self, request: Request) -> None:
        self._note(
            "requestfailed",
            method=request.method,
            path=urlsplit(request.url).path,
            failure=request.failure,
        )

    def response(self, response: Response) -> None:
        self._note(
            "response",
            method=response.request.method,
            path=urlsplit(response.url).path,
            status=response.status,
        )

    def console(self, message: ConsoleMessage) -> None:
        self._note("console", type=message.type, text=message.text[:CONSOLE_LIMIT])

    def navigated(self, frame: Frame) -> None:
        if frame == self.page.main_frame:
            self._note("framenavigated", path=urlsplit(frame.url).path)

    def attach(self) -> None:
        self.page.on("request", self.request)
        self.page.on("requestfailed", self.failed)
        self.page.on("response", self.response)
        self.page.on("console", self.console)
        self.page.on("framenavigated", self.navigated)

    def detach(self) -> None:
        # Playwright cannot remove a page "console" listener again (pyee
        # KeyError), so the recorder goes quiet instead; the page is per scene.
        self.active = False


def _probe(page: Page, script: str) -> Any:  # noqa: ANN401 - JSON page state
    """Evaluate a synchronous probe under wait_for_js's driver-side deadline.

    A plain ``page.evaluate`` has no deadline; a probe must never be the
    thing that hangs the suite.
    """
    return wait_for_js(page, script).json_value()


def _first_line(error: BaseException) -> str:
    text = str(error)
    return f"{type(error).__name__}: {text.splitlines()[0] if text else ''}"


@contextmanager
def disposal_diagnostics(page: Page, destination: Path) -> Iterator[None]:
    """Record one step's network, console and page state; write it only on failure.

    A passing step leaves nothing behind. On any failure the report goes to
    ``destination`` (0o600) and the original error propagates unchanged.
    """
    recorder = _StepRecorder(page)
    recorder.attach()
    try:
        _probe(page, POINTER_WATCH_JS)
        before = _probe(page, DISPOSAL_STATE_JS)
        try:
            yield
        except BaseException as error:
            try:
                after = _probe(page, DISPOSAL_STATE_JS)
            except PlaywrightError as problem:
                # A closed or crashed page must not mask the step's own error.
                after = {"unavailable": _first_line(problem)}
            report = {
                "error": _first_line(error),
                "elapsed": recorder.elapsed(),
                "before": before,
                "after": after,
                "events": recorder.events,
            }
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            destination.write_text(json.dumps(report, indent=2) + "\n")
            destination.chmod(0o600)
            raise
    finally:
        recorder.detach()


def diagnostics_path(root: Path, width: int) -> Path:
    return root / "retention" / f"retention-diagnostics-{width}.json"


def csrf(page: Page) -> str:
    """Read the CSRF token from the cookie jar; read-only pages have no form."""
    for cookie in page.context.cookies():
        if cookie["name"] == "csrftoken":
            token = str(cookie["value"])
            assert token
            return token
    token = page.locator('input[name="csrfmiddlewaretoken"]').first.input_value()
    assert token
    return token


def post_action(page: Page, url: str, fields: dict[str, str]) -> int:
    response = page.request.post(
        url,
        form={"csrfmiddlewaretoken": csrf(page), **fields},
        max_redirects=0,
    )
    return response.status


def seed_manager(staff: dict[str, str]) -> dict[str, str]:
    """Seed one clinic admin with a confirmed authenticator for this run."""
    manager = {
        "username": f"gestora-{secrets.token_hex(4)}",
        "id": str(uuid4()),
        "totp_key": secrets.token_hex(20),
    }
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant', %s, true)",
            [staff["organization"]],
        )
        conn.execute(
            "INSERT INTO clinic_app.identity_user "
            "(id, username, password, email, first_name, last_name, is_active, "
            "is_staff, is_superuser, date_joined) "
            "VALUES (%s, %s, %s, '', '', '', true, false, false, now())",
            [manager["id"], manager["username"], make_password(staff["password"])],
        )
        conn.execute(
            "INSERT INTO clinic_app.identity_userclinicrole "
            "(id, user_id, organization_id, clinic_id, role) "
            "VALUES (%s, %s, %s, %s, 'clinic_admin')",
            [str(uuid4()), manager["id"], staff["organization"], staff["clinic_a"]],
        )
        conn.execute("SET ROLE clinic_app")
        conn.execute(
            "SELECT set_config('app.current_user_id', %s, true)", [manager["id"]]
        )
        conn.execute(
            "INSERT INTO clinic_app.otp_totp_totpdevice "
            "(name, confirmed, key, step, t0, digits, tolerance, drift, last_t, "
            "user_id, throttling_failure_count, created_at) "
            "VALUES ('Clinic OS authenticator', true, %s, 30, 0, 6, 1, 0, -1, "
            "%s, 0, now())",
            [manager["totp_key"], manager["id"]],
        )
    return manager


def sign_in_manager(
    page: Page, base: str, staff: dict[str, str], manager: dict[str, str]
) -> None:
    # Independent scenes share one device; reset its replay marker so a
    # second sign-in inside the same 30-second window is never rejected.
    with psycopg.connect(staff["dsn"]) as connection:
        connection.execute("SET ROLE clinic_app")
        connection.execute(
            "SELECT set_config('app.current_user_id', %s, true)",
            [manager["id"]],
        )
        connection.execute(
            "UPDATE clinic_app.otp_totp_totpdevice SET last_t = -1, drift = 0 "
            "WHERE user_id = %s",
            [manager["id"]],
        )
    _sign_in(page, base, manager["username"], staff["password"])
    page.wait_for_url("**/auth/verify/**")
    # The next-window token is accepted through the device's tolerance and
    # stays valid for the whole submit, so a mid-flow window rollover can
    # never flake the challenge.
    token = TOTP(bytes.fromhex(manager["totp_key"]), 30, 0, 6, 1).token()
    page.locator("#id_otp_token").fill(f"{token:06d}")
    with page.expect_navigation():
        page.locator("button[type=submit]").click()
    page.wait_for_url("**/auth/protected/")


def grant_records(staff: dict[str, str], data: dict[str, str]) -> str:
    """Issue one records-capable invitation directly; return its code."""
    code = secrets.token_urlsafe(32)
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant', %s, true)",
            [staff["organization"]],
        )
        conn.execute(
            "INSERT INTO clinic_app.intake_patientaccessgrant "
            "(id,organization_id,clinic_id,patient_id,enrollment_id,issued_by_id,"
            "issued_by_label,secret_hash,operations,expires_at,created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,'Synthetic',%s,"
            "ARRAY['enrollment_view','records'],"
            "now()+interval '24 hours',now())",
            [
                str(uuid4()),
                staff["organization"],
                staff["clinic_a"],
                data["patient"],
                data["enrollment"],
                staff["receptionist_id"],
                hashlib.sha256(code.encode()).digest(),
            ],
        )
    return code


def stored_policies(
    staff: dict[str, str], manager_id: str
) -> list[tuple[str, int, str, str]]:
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant', %s, true)",
            [staff["organization"]],
        )
        return list(
            conn.execute(
                "SELECT record_class,version,state,id::text "
                "FROM clinic_app.retention_retentionpolicy "
                "WHERE proposed_by_id=%s ORDER BY record_class,version",
                [manager_id],
            ).fetchall()
        )


def stored_holds(staff: dict[str, str], record_id: str) -> list[tuple[str, str, bool]]:
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant', %s, true)",
            [staff["organization"]],
        )
        return list(
            conn.execute(
                "SELECT record_class,record_id::text,released_at IS NOT NULL "
                "FROM clinic_app.retention_legalhold WHERE record_id=%s "
                "ORDER BY created_at",
                [record_id],
            ).fetchall()
        )


def stored_hold_id(staff: dict[str, str], record_id: str) -> str:
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant', %s, true)",
            [staff["organization"]],
        )
        row = conn.execute(
            "SELECT id::text FROM clinic_app.retention_legalhold "
            "WHERE record_id=%s AND released_at IS NULL",
            [record_id],
        ).fetchone()
        assert row is not None
        return str(row[0])


def stored_exports(
    staff: dict[str, str], patient_id: str
) -> list[tuple[str, int, str]]:
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant', %s, true)",
            [staff["organization"]],
        )
        return list(
            conn.execute(
                "SELECT kind,record_count,manifest_digest "
                "FROM clinic_app.retention_recordexport WHERE patient_id=%s "
                "ORDER BY created_at",
                [patient_id],
            ).fetchall()
        )


def stored_version_state(staff: dict[str, str], version: str) -> str:
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant', %s, true)",
            [staff["organization"]],
        )
        row = conn.execute(
            "SELECT state FROM clinic_app.ehr_clinicaldocumentversion WHERE id=%s",
            [version],
        ).fetchone()
        assert row is not None
        return str(row[0])


def open_finalized(  # noqa: PLR0913 - the journey needs its full context
    page: Page,
    staff: dict[str, str],
    base: str,
    day: str,
    specialty: str,
    content: dict[str, str] | None = None,
) -> str:
    """Reach one finalized version through the real agenda journey."""
    _sign_in_physician(page, base, staff)
    page.goto(f"{base}/scheduling/clinics/{staff['clinic_a']}/agenda/day/{day}/1/")
    press(page, "open")
    page.locator("#template-id").select_option(specialty)
    press(page, "template")
    for field, value in (content or FIRST).items():
        page.locator(f"#id_{field}").fill(value)
    press(page, "save")
    press_in_view(page, "finalize")
    expect(page.locator("[data-version]")).to_have_attribute("data-state", "finalized")
    version = page.locator("[data-version]").get_attribute("data-version")
    assert version is not None
    return version


def verify_package(data: bytes, expected_record: str) -> dict[str, object]:
    """Recompute the manifest digest and file digests of one package."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = archive.namelist()
        assert names.count("manifest.json") == 1
        manifest: dict[str, Any] = json.loads(archive.read("manifest.json"))
        files = {name: archive.read(name) for name in names if name != "manifest.json"}
    entries = manifest["files"]
    assert {entry["path"] for entry in entries} == set(files)
    for entry in entries:
        assert hashlib.sha256(files[entry["path"]]).hexdigest() == entry["sha256"]
    claimed = manifest["manifest_sha256"]
    recomputed = hashlib.sha256(
        rfc8785.dumps({k: v for k, v in manifest.items() if k != "manifest_sha256"})
    ).hexdigest()
    assert recomputed == claimed
    assert manifest["record_count"] == len(entries)
    assert entries[0]["record_id"] == expected_record
    return manifest


def _physician_scene(  # noqa: PLR0913 - the scene needs its full context
    page: Page,
    staff: dict[str, str],
    base: str,
    root: Path,
    width: int,
    data: dict[str, str],
    url: str,
) -> str:
    """Finalize, release and export one record as the assigned physician."""
    version = open_finalized(page, staff, base, DAYS[width], data["specialty"])
    page.goto(url)
    expect(page.locator("#policy-form")).to_have_count(0)
    expect(page.locator("#hold-form")).to_have_count(0)
    expect(page.locator("#releasable-list")).to_be_visible()
    capture(page, root, "physician-status", width)
    press(page, "release")
    expect(page.locator("#release-list")).to_be_visible()
    capture(page, root, "released", width)
    with page.expect_download() as received:
        click_when_hittable(
            page.locator(
                f'#export-patient-list li[data-patient="{data["patient"]}"] '
                'button[value="export"]'
            )
        )
    package = received.value
    package_path = package.path()
    assert package_path is not None
    manifest = verify_package(package_path.read_bytes(), version)
    assert manifest["kind"] == "staff"
    assert stored_exports(staff, data["patient"]) == [
        ("staff", 1, manifest["manifest_sha256"])
    ]
    # An unknown version can never be released through the workspace.
    status = post_action(page, url, {"action": "release", "version_id": str(uuid4())})
    assert status == 403
    # The foreign clinic workspace is non-enumerating.
    foreign = page.request.get(f"{base}/retention/clinics/{staff['clinic_b']}/")
    assert foreign.status == 403
    return version


def _policy_scene(
    page: Page,
    staff: dict[str, str],
    manager: dict[str, str],
    root: Path,
    width: int,
) -> None:
    """Propose and approve one zero-day policy through the manager forms."""
    page.locator("#policy-form #id_record_class").select_option("ehr.document_version")
    page.locator("#policy-form #id_retention_days").fill("0")
    press_in_view(page, "propose_policy")
    proposed = stored_policies(staff, manager["id"])
    assert [(row[0], row[2]) for row in proposed] == [
        ("ehr.document_version", "proposed")
    ]
    policy_id = proposed[0][3]
    row = page.locator(f'#policy-list li[data-policy="{policy_id}"]')
    expect(row).to_have_attribute("data-state", "proposed")
    row.locator('button[value="approve_policy"]').click()
    page.wait_for_url("**/retention/**")
    expect(
        page.locator(f'#policy-list li[data-policy="{policy_id}"]')
    ).to_have_attribute("data-state", "approved")
    assert [(row[0], row[2]) for row in stored_policies(staff, manager["id"])] == [
        ("ehr.document_version", "approved")
    ]
    capture(page, root, "policy-approved", width)


def _hold_scene(  # noqa: PLR0913 - the scene needs its full context
    page: Page,
    staff: dict[str, str],
    root: Path,
    width: int,
    version: str,
    errors: list[str],
) -> None:
    """Hold the record, prove disposal is denied, then release the hold."""
    page.locator("#hold-form #id_record_class").select_option("ehr.document_version")
    page.locator("#hold-form #id_record_id").fill(version)
    page.locator("#hold-form #id_authority").fill("Autoridade sintética")
    page.locator("#hold-form #id_reason").fill("Motivo sintético")
    press_in_view(page, "place_hold")
    expect(page.locator("#hold-list")).to_contain_text("ativa")
    assert stored_holds(staff, version) == [("ehr.document_version", version, False)]
    capture(page, root, "hold-active", width)
    page.locator("#disposal-form #id_record_class").select_option(
        "ehr.document_version"
    )
    page.locator("#disposal-form #id_record_id").fill(version)
    with disposal_diagnostics(page, diagnostics_path(root, width)):
        press_in_view(page, "check_disposal")
    expect(page.locator("#retention-error")).to_contain_text("Descarte negado")
    _consume_expected_error(errors, "403")
    assert stored_version_state(staff, version) == "finalized"
    capture(page, root, "disposal-denied", width)
    page.locator("#hold-release-form #id_hold_id").fill(stored_hold_id(staff, version))
    page.locator("#hold-release-form #id_release_authority").fill(
        "Autoridade de liberação"
    )
    page.locator("#hold-release-form #id_release_reason").fill("Motivo de liberação")
    press_in_view(page, "release_hold")
    expect(page.locator("#hold-list")).to_contain_text("liberada")
    page.locator("#disposal-form #id_record_class").select_option(
        "ehr.document_version"
    )
    page.locator("#disposal-form #id_record_id").fill(version)
    with disposal_diagnostics(page, diagnostics_path(root, width)):
        press_in_view(page, "check_disposal")
    expect(page.locator("#disposal-result")).to_contain_text("Elegível")
    capture(page, root, "disposal-eligible", width)


def _reception_scene(  # noqa: PLR0913 - the scene needs its full context
    page: Page,
    staff: dict[str, str],
    base: str,
    root: Path,
    width: int,
    version: str,
    url: str,
) -> None:
    """Reception reads the status page but every write is denied."""
    _sign_in_receptionist(page, base, staff)
    response = page.goto(url)
    assert response is not None
    assert response.status == 200
    expect(page.locator("#policy-form")).to_have_count(0)
    expect(page.locator("#hold-form")).to_have_count(0)
    expect(page.locator("#releasable-list")).to_have_count(0)
    capture(page, root, "reception-readonly", width)
    status = post_action(
        page,
        url,
        {
            "action": "place_hold",
            "record_class": "ehr.document_version",
            "record_id": version,
            "authority": "x",
            "reason": "y",
        },
    )
    assert status == 403


def _patient_scene(  # noqa: PLR0913 - the scene needs its full context
    page: Page,
    staff: dict[str, str],
    base: str,
    root: Path,
    width: int,
    data: dict[str, str],
    version: str,
) -> None:
    """Redeem a records invitation and download the verified package."""
    code = grant_records(staff, data)
    _redeem(page, base, staff["clinic_a"], code)
    page.wait_for_url("**/patient/")
    page.goto(f"{base}/patient/records/")
    expect(page.locator("h1")).to_have_text("Seus registros")
    assert FIRST["subjective"] in page.content()
    capture(page, root, "patient-records", width)
    with page.expect_download() as received:
        page.locator("#export-button").click()
    package = received.value
    package_path = package.path()
    assert package_path is not None
    manifest = verify_package(package_path.read_bytes(), version)
    assert manifest["kind"] == "patient"
    kinds = [row[0] for row in stored_exports(staff, data["patient"])]
    assert kinds == ["staff", "patient"]


@pytest.mark.parametrize("width", [1280, 768, 375])
def test_retention_journey(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    width: int,
) -> None:
    staff, base, root = availability_staff, renewal_base_url, renewal_artifact_root
    data = seed(staff, DAYS[width])
    manager = seed_manager(staff)
    browser = renewal_page.context.browser
    assert browser is not None
    context = browser.new_context(
        locale="pt-BR", viewport={"width": width, "height": 900}
    )
    page = context.new_page()
    persona_errors: dict[str, list[str]] = {"physician": _watch_errors(page)}
    url = f"{base}/retention/clinics/{staff['clinic_a']}/"
    try:
        version = _physician_scene(page, staff, base, root, width, data, url)
        manager_context = browser.new_context(
            locale="pt-BR", viewport={"width": width, "height": 900}
        )
        try:
            manager_page = manager_context.new_page()
            persona_errors["manager"] = _watch_errors(manager_page)
            sign_in_manager(manager_page, base, staff, manager)
            manager_page.goto(url)
            expect(manager_page.locator("#policy-form")).to_be_visible()
            _policy_scene(manager_page, staff, manager, root, width)
            _hold_scene(
                manager_page, staff, root, width, version, persona_errors["manager"]
            )
        finally:
            manager_context.close()
        denied_context = browser.new_context(
            locale="pt-BR", viewport={"width": width, "height": 900}
        )
        try:
            reception_page = denied_context.new_page()
            persona_errors["reception"] = _watch_errors(reception_page)
            _reception_scene(reception_page, staff, base, root, width, version, url)
        finally:
            denied_context.close()
        patient_context = browser.new_context(
            locale="pt-BR", viewport={"width": width, "height": 900}
        )
        try:
            patient_page = patient_context.new_page()
            persona_errors["patient"] = _watch_errors(patient_page)
            _patient_scene(
                patient_page,
                staff,
                base,
                root,
                width,
                data,
                version,
            )
        finally:
            patient_context.close()
        assert not any(persona_errors.values()), persona_errors
        (root / "retention" / f"report-{width}.json").write_text(
            json.dumps(
                {
                    "width": width,
                    "console_errors": persona_errors,
                    "released_version": version,
                    "staff_export_kind": "staff",
                    "patient_export_kind": "patient",
                    "disposal_denied_held": True,
                    "disposal_eligible_after_release": True,
                    "reception_status": 200,
                    "reception_write_status": 403,
                    "foreign_clinic_status": 403,
                    "unknown_release_status": 403,
                    "runtime": "clinic_app",
                },
                indent=2,
            )
            + "\n"
        )
        # The check_disposal collector ran and, on success, wrote nothing.
        assert not diagnostics_path(root, width).exists()
    finally:
        context.close()


class _ForcedStepFailureError(Exception):
    """Stands in for a step that fails, to prove the collector's failure path."""

    def __init__(self) -> None:
        """Carry the fixed marker the report must name."""
        super().__init__("forced")


def _failing_step(page: Page, destination: Path, url: str) -> None:
    with disposal_diagnostics(page, destination):
        page.goto(url)
        raise _ForcedStepFailureError


def test_disposal_diagnostics_are_written_only_on_failure(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
) -> None:
    folder = renewal_artifact_root / "retention" / "diagnostics-self-test"
    login = f"{renewal_base_url}/auth/login/"
    renewal_page.goto(login)
    passed = folder / "passed.json"
    with disposal_diagnostics(renewal_page, passed):
        renewal_page.goto(login)
    assert not passed.exists()
    assert not folder.exists()
    failed = folder / "failed.json"
    with pytest.raises(_ForcedStepFailureError, match="forced"):
        _failing_step(renewal_page, failed, login)
    report = json.loads(failed.read_text())
    assert failed.stat().st_mode & 0o777 == 0o600
    assert report["error"] == "_ForcedStepFailureError: forced"
    for state in (report["before"], report["after"]):
        assert state["path"] == "/auth/login/"
        assert state["readyState"] == "complete"
        assert state["button"] is None
        assert set(state["serviceWorker"]) == {
            "controlled",
            "controller",
            "registration",
        }
    # The watcher is armed in the step's document; the navigation replaced it.
    assert report["before"]["pointer"] == []
    assert report["after"]["pointer"] is None
    assert report["before"]["serviceWorker"]["registration"] is not None
    assert report["after"]["serviceWorker"]["registration"] is None
    # Subresources of the page loaded before the step may still report.
    first = next(e for e in report["events"] if e.get("navigation"))
    assert {k: first[k] for k in ("method", "path", "type")} == {
        "method": "GET",
        "path": "/auth/login/",
        "type": "document",
    }
    assert any(
        e["kind"] == "response" and e["path"] == "/auth/login/" and e["status"] == 200
        for e in report["events"]
    )
    assert any(e["kind"] == "framenavigated" for e in report["events"])
    # A forced failure is not evidence; leave nothing for the upload.
    shutil.rmtree(folder)


# --------------------------------------------------------------------------
# Accessibility/state matrix and native form fallback
# --------------------------------------------------------------------------

LONG_SOAP = {
    "subjective": "Relato sintético extenso " + "conteúdo longitudinal " * 12,
    "objective": "Exame sintético extenso " + "observação clínica " * 12,
    "assessment": "Avaliação sintética extensa " + "parecer detalhado " * 10,
    "plan": "Plano sintético extenso " + "conduta registrada " * 12,
}
LONG_AUTHORITY = "Autoridade sintética " + "comarca seção " * 10
LONG_REASON = "Motivo sintético " + "fundamentação legal " * 10
MATRIX_SCENES: tuple[tuple[str, dict[str, Any]], ...] = (
    ("widths", {"viewport": {"width": 1280, "height": 900}}),
    (
        "forced_colors",
        {"viewport": {"width": 1280, "height": 900}, "forced_colors": "active"},
    ),
    (
        "reduced_motion",
        {"viewport": {"width": 375, "height": 900}, "reduced_motion": "reduce"},
    ),
    (
        "zoom_200",
        {
            "viewport": {
                "width": ZOOM_WINDOW // ZOOM_FACTOR,
                "height": 900 // ZOOM_FACTOR,
            },
            "device_scale_factor": ZOOM_FACTOR,
        },
    ),
)


def _matrix_patient(
    browser: Browser,
    options: dict[str, Any],
    staff: dict[str, str],
    base: str,
    data: dict[str, str],
) -> tuple[Page, list[str], BrowserContext]:
    """Open one patient context, redeem a records grant and watch its console."""
    context = new_context(browser, locale="pt-BR", **options)
    page = context.new_page()
    page.set_default_timeout(20_000)
    errors = _watch_errors(page)
    code = grant_records(staff, data)
    _redeem(page, base, staff["clinic_a"], code)
    page.wait_for_url("**/patient/")
    page.goto(f"{base}/patient/records/")
    return page, errors, context


def _consume_expected_error(errors: list[str], status: str) -> None:
    """Remove the one console error the asserted denial produced.

    Firefox logs no failed response (engines.failed_responses_logged), so
    there the denial must have left no console line at all.
    """
    if not failed_responses_logged():
        assert not [e for e in errors if status in e], errors
        return
    expected = next((e for e in errors if status in e), None)
    assert expected is not None, errors
    errors.remove(expected)


def _matrix_widths(  # noqa: PLR0913 - the scene needs its full context
    page: Page,
    patient: Page,
    empty_patient: Page,
    staff: dict[str, str],
    root: Path,
    url: str,
    version: str,
    errors: list[str],
) -> dict[str, object]:
    """States, 320px reflow, long content and keyboard on both surfaces."""
    # Empty state: a records session whose patient has no release yet.
    expect(empty_patient.locator("#records-empty")).to_be_visible()
    capture(empty_patient, root, "matrix-empty", 1280)
    # Error state: an invalid hold form renders the alert, writes nothing.
    page.locator("#hold-form #id_record_class").select_option("ehr.document_version")
    page.locator("#hold-form #id_record_id").fill("not-a-uuid")
    page.locator("#hold-form #id_authority").fill("Autoridade")
    page.locator("#hold-form #id_reason").fill("Motivo")
    press(page, "place_hold")
    expect(page.locator("#hold-form .feedback--error")).to_be_visible()
    _consume_expected_error(errors, "400")
    capture(page, root, "matrix-error", 1280)
    # Long content: a 255-character authority/reason pair stays inside.
    page.locator("#hold-form #id_record_id").fill(version)
    page.locator("#hold-form #id_authority").fill(LONG_AUTHORITY)
    page.locator("#hold-form #id_reason").fill(LONG_REASON)
    press(page, "place_hold")
    expect(page.locator("#hold-list")).to_contain_text("ativa")
    capture(page, root, "matrix-long-content", 1280)
    # 320px reflow on the populated workspace and the long patient record.
    page.set_viewport_size({"width": 320, "height": 900})
    patient.set_viewport_size({"width": 320, "height": 900})
    assert _no_overflow(page), _overflowing(page)
    assert _no_overflow(patient), _overflowing(patient)
    capture(page, root, "matrix-reflow", 320)
    capture(patient, root, "matrix-patient-reflow", 320)
    page.set_viewport_size({"width": 1280, "height": 900})
    patient.set_viewport_size({"width": 1280, "height": 900})
    # Keyboard: Enter submits the policy form and the patient export.
    focused: list[str] = []
    _tab_until(page, page.locator('button[value="propose_policy"]'), focused)
    with page.expect_navigation():
        page.keyboard.press("Enter")
    expect(page.locator(".feedback--success").first).to_be_visible()
    capture(page, root, "matrix-success", 1280)
    _tab_until(patient, patient.locator("#export-button"), focused)
    with patient.expect_download() as received:
        patient.keyboard.press("Enter")
    assert received.value.path() is not None
    return {
        "empty_state": True,
        "error_state": True,
        "success_state": True,
        "long_content": {
            "authority_chars": len(LONG_AUTHORITY),
            "reason_chars": len(LONG_REASON),
            "subjective_chars": len(LONG_SOAP["subjective"]),
        },
        "reflow_width": 320,
        "keyboard": {"manager_submit": True, "patient_export": True},
        "tab_stops": focused,
    }


def _matrix_denied(  # noqa: PLR0913 - the scene needs its full context
    browser: Browser,
    options: dict[str, Any],
    staff: dict[str, str],
    base: str,
    root: Path,
    url: str,
    version: str,
) -> dict[str, object]:
    """Reception reads the workspace but every write stays denied."""
    context = new_context(browser, locale="pt-BR", **options)
    try:
        page = context.new_page()
        page.set_default_timeout(20_000)
        errors = _watch_errors(page)
        _sign_in_receptionist(page, base, staff)
        response = page.goto(url)
        assert response is not None
        assert response.status == 200
        expect(page.locator("#policy-form")).to_have_count(0)
        capture(page, root, "matrix-denied", 1280)
        status = post_action(
            page,
            url,
            {
                "action": "place_hold",
                "record_class": "ehr.document_version",
                "record_id": version,
                "authority": "x",
                "reason": "y",
            },
        )
        assert status == 403
        assert not errors, errors
    finally:
        context.close()
    return {"read_status": 200, "write_status": 403}


def _matrix_forced_colors(page: Page, patient: Page, root: Path) -> dict[str, object]:
    """Focus rings survive forced colors on both surfaces."""
    assert page.evaluate("matchMedia('(forced-colors: active)').matches")
    assert patient.evaluate("matchMedia('(forced-colors: active)').matches")
    page.keyboard.press("Tab")
    ring = _ring(page)
    assert ring["style"] == "solid", ring
    patient.keyboard.press("Tab")
    patient_ring = _ring(patient)
    assert patient_ring["style"] == "solid", patient_ring
    capture(page, root, "matrix-forced-colors", 1280)
    capture(patient, root, "matrix-patient-forced-colors", 1280)
    return {"staff_ring": ring, "patient_ring": patient_ring}


def _matrix_reduced_motion(page: Page, patient: Page, root: Path) -> dict[str, object]:
    """Both surfaces drop every transition under reduced motion."""
    transition = page.evaluate(
        "getComputedStyle(document.querySelector('.button')).transitionDuration"
    )
    patient_transition = patient.evaluate(
        "getComputedStyle(document.querySelector('.button')).transitionDuration"
    )
    assert transition == "0s", transition
    assert patient_transition == "0s", patient_transition
    capture(patient, root, "matrix-reduced-motion", 375)
    return {
        "staff_transition": transition,
        "patient_transition": patient_transition,
    }


def _matrix_zoom(page: Page, patient: Page, root: Path) -> dict[str, object]:
    """A 1280px window at 200% zoom: 640 CSS px at device ratio 2."""
    metrics = {
        "staff": dict(
            page.evaluate(
                "({device_pixel_ratio: devicePixelRatio,"
                " css_viewport_width: innerWidth})"
            )
        ),
        "patient": dict(
            patient.evaluate(
                "({device_pixel_ratio: devicePixelRatio,"
                " css_viewport_width: innerWidth})"
            )
        ),
    }
    for label, surface in (("staff", page), ("patient", patient)):
        values = metrics[label]
        assert values["device_pixel_ratio"] == ZOOM_FACTOR
        assert values["css_viewport_width"] == ZOOM_WINDOW // ZOOM_FACTOR
        assert _no_overflow(surface), _overflowing(surface)
    height = float(
        page.evaluate(
            "document.querySelector('.button').getBoundingClientRect().height"
        )
    )
    assert height >= MIN_TARGET_PX, height
    capture(page, root, "matrix-zoom-200", 640)
    capture(patient, root, "matrix-patient-zoom-200", 640)
    return {**metrics, "window_width": ZOOM_WINDOW, "button_height": height}


def _matrix_conflict(  # noqa: PLR0913 - the scene needs its full context
    page: Page,
    root: Path,
    url: str,
    second_version: str,
    draft: str,
    errors: list[str],
) -> dict[str, object]:
    """Releasing a draft through a forged id renders the fixed 409 surface."""
    page.goto(url)
    row = page.locator(f'#releasable-list li[data-version="{second_version}"]')
    row.locator('input[name="version_id"]').evaluate(
        "(element, value) => { element.value = value; }", draft
    )
    with page.expect_navigation():
        row.locator('button[value="release"]').click()
    expect(page.locator("#retention-error")).to_contain_text("finalizada")
    _consume_expected_error(errors, "409")
    capture(page, root, "matrix-conflict", 1280)
    return {"status": 409, "rendered": True}


def _matrix_setup(  # noqa: PLR0913 - the scene needs its full context
    browser: Browser,
    staff: dict[str, str],
    base: str,
    root: Path,
    url: str,
    data: dict[str, str],
    second: dict[str, str],
) -> tuple[str, list[str]]:
    """Finalize, release and amend real records; render the 409 conflict."""
    context = browser.new_context(
        locale="pt-BR", viewport={"width": 1280, "height": 900}
    )
    try:
        page = context.new_page()
        errors = _watch_errors(page)
        first = open_finalized(
            page, staff, base, "2035-06-05", data["specialty"], LONG_SOAP
        )
        page.goto(url)
        # Release the first version; the second stays releasable for the
        # forged conflict below.
        with page.expect_navigation():
            page.locator(
                f'#releasable-list li[data-version="{first}"] button[value="release"]'
            ).click()
        second_version = open_finalized(
            page, staff, base, "2035-06-06", second["specialty"]
        )
        page.locator("#id_reason").fill("Retificação sintética")
        press(page, "amend")
        expect(page.locator("[data-version]")).to_have_attribute("data-state", "draft")
        draft = page.locator("[data-version]").get_attribute("data-version")
        assert draft is not None
        _matrix_conflict(page, root, url, second_version, draft, errors)
        assert not errors, errors
    finally:
        context.close()
    return first, errors


def _matrix_scene(  # noqa: PLR0913 - the scene needs its full context
    browser: Browser,
    scene: str,
    options: dict[str, Any],
    staff: dict[str, str],
    manager: dict[str, str],
    base: str,
    root: Path,
    url: str,
    data: dict[str, str],
    second: dict[str, str],
    first: str,
) -> tuple[dict[str, object], dict[str, object], dict[str, list[str]]]:
    """Run one matrix scene; return its report, states and console errors."""
    scene_report: dict[str, object] = {}
    states: dict[str, object] = {}
    console: dict[str, list[str]] = {}
    context = new_context(browser, locale="pt-BR", **options)
    try:
        page = context.new_page()
        page.set_default_timeout(20_000)
        errors = _watch_errors(page)
        sign_in_manager(page, base, staff, manager)
        page.goto(url)
        patient, patient_errors, patient_context = _matrix_patient(
            browser, options, staff, base, data
        )
        try:
            if scene == "widths":
                empty, empty_errors, empty_context = _matrix_patient(
                    browser, options, staff, base, second
                )
                try:
                    scene_report = _matrix_widths(
                        page, patient, empty, staff, root, url, first, errors
                    )
                finally:
                    empty_context.close()
                assert not empty_errors, empty_errors
                states["denied"] = _matrix_denied(
                    browser, options, staff, base, root, url, first
                )
            elif scene == "forced_colors":
                scene_report = _matrix_forced_colors(page, patient, root)
            elif scene == "reduced_motion":
                scene_report = _matrix_reduced_motion(page, patient, root)
            else:
                scene_report = _matrix_zoom(page, patient, root)
        finally:
            patient_context.close()
        assert not patient_errors, (scene, patient_errors)
        console[f"{scene}-manager"] = errors
        console[f"{scene}-patient"] = patient_errors
    finally:
        context.close()
    assert not errors, (scene, errors)
    return scene_report, states, console


def test_retention_accessibility_matrix(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
) -> None:
    """The required matrix on the real manager and patient surfaces.

    320px reflow, 200% zoom, forced colors, reduced motion, keyboard
    activation, long content and the default/empty/error/denied/success/
    conflict states; every persona page reports its own console errors.
    """
    staff, base, root = availability_staff, renewal_base_url, renewal_artifact_root
    data = seed(staff, "2035-06-05")
    second = seed(staff, "2035-06-06")
    manager = seed_manager(staff)
    browser = renewal_page.context.browser
    assert browser is not None
    url = f"{base}/retention/clinics/{staff['clinic_a']}/"
    report: dict[str, object] = {"states": {}, "console_errors": {}}
    console = report["console_errors"]
    assert isinstance(console, dict)
    first, console["physician"] = _matrix_setup(
        browser, staff, base, root, url, data, second
    )
    states = report["states"]
    assert isinstance(states, dict)
    states["conflict"] = {"status": 409, "rendered": True}
    for scene, options in MATRIX_SCENES:
        scene_report, scene_states, scene_console = _matrix_scene(
            browser,
            scene,
            options,
            staff,
            manager,
            base,
            root,
            url,
            data,
            second,
            first,
        )
        report[scene] = scene_report
        states.update(scene_states)
        console.update(scene_console)
    report["not_applicable"] = {
        "loading": (
            "every retention surface is a complete server-rendered document;"
            " no deferred or streaming region exists"
        ),
        "dragging": ("no drag interaction exists; every action is a button or form"),
    }
    destination = root / "retention" / "accessibility-report.json"
    destination.parent.mkdir(mode=0o700, exist_ok=True)
    destination.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    destination.chmod(0o600)


def test_retention_native_form_fallback(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
) -> None:
    """Every retention action is a native POST; no JavaScript is required."""
    staff, base, root = availability_staff, renewal_base_url, renewal_artifact_root
    data = seed(staff, "2035-06-07")
    browser = renewal_page.context.browser
    assert browser is not None
    context = browser.new_context(
        java_script_enabled=False,
        locale="pt-BR",
        viewport={"width": 375, "height": 900},
    )
    try:
        page = context.new_page()
        version = open_finalized(page, staff, base, "2035-06-07", data["specialty"])
        url = f"{base}/retention/clinics/{staff['clinic_a']}/"
        page.goto(url)
        with page.expect_navigation():
            click_when_hittable(
                page.locator(
                    f'#releasable-list li[data-version="{version}"] '
                    'button[value="release"]'
                )
            )
        expect(page.locator("#release-list")).to_be_visible()
        code = grant_records(staff, data)
        _redeem(page, base, staff["clinic_a"], code)
        page.wait_for_url("**/patient/")
        page.goto(f"{base}/patient/records/")
        expect(page.locator("h1")).to_have_text("Seus registros")
        assert FIRST["subjective"] in page.content()
        with page.expect_download() as received:
            page.locator("#export-button").click()
        package_path = received.value.path()
        assert package_path is not None
        manifest = verify_package(package_path.read_bytes(), version)
        assert manifest["kind"] == "patient"
        capture(page, root, "native-no-javascript", 375)
    finally:
        context.close()
