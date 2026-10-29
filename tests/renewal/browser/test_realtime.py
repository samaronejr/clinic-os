"""Two-context real SSE, forced process loss, replay, revocation and a11y."""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Final
from urllib.parse import urlparse
from uuid import uuid4

import psycopg
from django.contrib.auth.hashers import make_password
from django.utils.translation import gettext
from playwright.sync_api import expect, sync_playwright

from renewal.browser._fixture_secrets import new_totp_key, worker_dsn
from renewal.browser._navigation import (
    click_to_navigate,
    expect_document,
    goto_settled,
)
from renewal.browser._page_wait import wait_for_js
from renewal.browser._teleconsult import register_physician
from renewal.browser.engines import (
    full_page_screenshot,
    launch_selected,
    watch_page_errors,
)
from renewal.browser.test_agenda import (
    BOOKING_SUBMIT,
    DAYS,
    ROWS,
    SETTLED_JS,
    _agenda_path,
    _fill_booking,
    _open_booking,
    _promise,
    _seed_enrollment,
    _sign_in_receptionist,
    agenda_staff,
)
from renewal.browser.test_availability import _sign_in_physician
from renewal.browser.test_clinician_video import (
    _Case,
    _context,
    _enter,
    _htmx,
    _seed_patient,
)
from renewal.browser.test_passkeys import _organization_admin
from renewal.browser.test_patient_access import _redeem
from renewal.browser.test_primitives import AXE_RUN_JS
from renewal.browser.test_retention import seed_manager, sign_in_manager
from renewal.browser.test_teleconsult import (
    _accept_consent,
    _create_session,
    _publish_consent,
    _room_operation,
    _worker,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from playwright.sync_api import (
        Browser,
        ConsoleMessage,
        Page,
        Request,
        Response,
        Route,
    )

__all__ = ("agenda_staff",)
WIDTHS = (1280, 375, 320)
TELECONSULT_DAY = "2035-08-12"
# Ticket POST and EventSource GET share this one same-origin path.
REALTIME_PATH: Final = "/rt/stream"


class _RealtimeTap:
    """Every /rt/stream request and response of one page, against a revocation.

    Boundary rule, causal and clock-free: ``mark_boundary`` runs BEFORE the
    owner transaction that revokes the role commits, and freezes the list of
    realtime responses the tap has received so far. Only those responses are
    pre-revocation; every later one is post-revocation. The server sends a
    response only after deciding it, the tap receives it only after it is
    sent, and the commit happens after the mark, so a response the server
    decides after the commit can never be in the frozen list, whatever the
    browser or host clocks say. The rule fails closed: a response decided
    before the commit but received after the mark is post-revocation too, and
    a 2xx one fails the test. The mark first waits for the response of every
    realtime request the tap has seen, so an idle page leaves nothing
    legitimately in flight across the commit.
    """

    def __init__(self) -> None:
        self.boundary: int | None = None
        self.requests: list[Request] = []
        self.responses: list[Response] = []
        self.streams: list[Request] = []
        self.open_streams: list[Request] = []

    def mark_boundary(self, page: Page) -> None:
        for request in self.requests:
            answered = any(seen.request is request for seen in self.responses)
            if not answered and request.failure is None:
                # Registered before any event is dispatched, so the response
                # cannot slip past; bounded by the page default timeout.
                with page.expect_response(self._answers(request)):
                    pass
        self.boundary = len(self.responses)

    @staticmethod
    def _answers(request: Request) -> Callable[[Response], bool]:
        return lambda response: response.request is request

    def on_request(self, request: Request) -> None:
        if urlparse(request.url).path != REALTIME_PATH:
            return
        self.requests.append(request)
        if request.method == "GET" and "text/event-stream" in request.headers.get(
            "accept", ""
        ):
            self.streams.append(request)
            self.open_streams.append(request)

    def on_response(self, response: Response) -> None:
        if urlparse(response.request.url).path == REALTIME_PATH:
            self.responses.append(response)

    def on_ended(self, request: Request) -> None:
        # requestfinished or requestfailed: the EventSource request is over.
        self.open_streams = [
            open_ for open_ in self.open_streams if open_ is not request
        ]

    def is_refusal(self, response: Response) -> bool:
        # Identity against the frozen pre-boundary prefix: independent of
        # whether the tap's listener has already appended this response.
        return (
            urlparse(response.url).path == REALTIME_PATH
            and self.boundary is not None
            and all(seen is not response for seen in self.responses[: self.boundary])
            and response.status == 403
        )

    def post_revocation(self) -> list[tuple[str, int]]:
        assert self.boundary is not None
        return [
            (response.request.method, response.status)
            for response in self.responses[self.boundary :]
        ]

    def grants(self) -> list[tuple[str, int]]:
        return [
            (method, status)
            for method, status in self.post_revocation()
            if 200 <= status < 300
        ]


def _connected(page: Page) -> None:
    expect(page.locator("#agenda-shell")).to_have_attribute(
        "data-realtime-state", "connected"
    )
    wait_for_js(page, SETTLED_JS)


def _accessibility(page: Page, root: Path, width: int) -> None:
    page.add_script_tag(url="/static/vendor/axe/axe.min.js")
    violations = page.evaluate(AXE_RUN_JS)
    (root / f"axe-{width}.json").write_text(json.dumps(violations))
    assert violations == []
    small = page.locator(
        "main a, main button, main input:not([type=hidden]), main select, main summary"
    ).evaluate_all("""nodes => nodes.filter(n => {
      const r = n.getBoundingClientRect();
      return r.width > 0 && r.height > 0 && (r.width < 44 || r.height < 44);
    }).map(n => n.tagName)""")
    assert small == []
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    full_page_screenshot(page, root / f"agenda-{width}.png")


def _ticket_replay(page: Page) -> None:
    # Keep the credential in page memory; never in reports or logs.
    # cache:'no-store' on both GETs: Firefox queues a same-URL request behind
    # an open HTTP-cache entry writer, so a default-mode replay waits until the
    # stream ends (on the server's 600 s cap, or a network-change abort that
    # also drops the page's EventSource; fix-a16). The replay must reach the
    # server while the first stream is still open.
    status = page.evaluate("""async () => {
      const csrf = document.querySelector('[name=csrfmiddlewaretoken]').value;
      const topic = document.querySelector('[data-realtime-topic]')
        .dataset.realtimeTopic;
      const response = await fetch('/rt/stream', {
        method:'POST',
        headers:{'Content-Type':'application/json','X-CSRFToken':csrf},
        body:JSON.stringify({topics:[topic]})
      });
      const ticket = (await response.json()).ticket;
      const endpoint = '/rt/stream?t=' + ticket;
      const controller = new AbortController();
      const stream = await fetch(endpoint,
        {signal:controller.signal, cache:'no-store'});
      await stream.body.getReader().read();
      const replay = await fetch(endpoint, {cache:'no-store'});
      controller.abort();
      return [stream.status, replay.status];
    }""")
    assert status == [200, 403]


def _degraded(page: Page, base_url: str, staff: dict[str, str], root: Path) -> None:
    _sign_in_receptionist(page, base_url, staff)
    goto_settled(page, base_url + f"/scheduling/clinics/{staff['clinic_b']}/agenda/")
    _connected(page)
    page.clock.install()
    assert page.request.post(base_url + "/_realtime-test/stop").status == 204
    expect(page.locator("[data-realtime-unavailable]")).to_be_visible()
    expect(page.locator("[data-realtime-unavailable]")).to_have_text(
        gettext("Realtime updates unavailable")
    )
    with page.expect_response(
        lambda response: response.request.headers.get("hx-request") == "true"
    ) as polling:
        page.clock.fast_forward(30000)
    assert polling.value.status == 200
    full_page_screenshot(page, root / "degraded.png")


def _denied_after_revocation(
    a: Page,
    b: Page,
    base_url: str,
    agenda_staff: dict[str, str],
    tap: _RealtimeTap,
) -> list[tuple[str, int]]:
    # Owner SQL revocation invalidates every session for this account.
    # The next staff request flushes the revoked session and publishes its
    # immediate control event; unit tests also prove periodic reauthorization.
    # The refusal wait is armed before the revocation: b's stream can also
    # close for other reasons (a transport drop and its backoff retry, the
    # periodic reauthorization), and a refused request that lands before a
    # late wait exists is lost (hosted run 36475967514; fix-a16). It matches
    # only a post-revocation 403, so a late or absent refusal times out.
    # Stream closure starts both a ticket POST and a clinical refetch.
    # The refetch can reach login first, whose pagehide aborts that POST.
    # Hold only the real HTMX refetch until the real ticket refusal is seen;
    # then forward it unchanged and observe the terminal login navigation.
    refetch_url = b.url
    refetches: list[Route] = []

    def defer_refetch(route: Route) -> None:
        if route.request.headers.get("hx-request") == "true":
            refetches.append(route)
        else:
            route.continue_()

    with expect_document(b, url=base_url + "/auth/login/"):
        b.route(refetch_url, defer_refetch)
        try:
            with b.expect_response(tap.is_refusal):
                # The mark precedes the commit, so every later server decision
                # is post-revocation (gate-review r2 B1: no clock boundary).
                tap.mark_boundary(b)
                with psycopg.connect(agenda_staff["dsn"]) as owner:
                    owner.execute(
                        "SELECT set_config('app.current_tenant', %s, true)",
                        [agenda_staff["organization"]],
                    )
                    owner.execute(
                        "UPDATE clinic_app.identity_userclinicrole "
                        "SET revoked_at=now() WHERE clinic_id = %s AND user_id = %s",
                        [agenda_staff["clinic_a"], agenda_staff["receptionist_id"]],
                    )
                # Leaving the connection context committed the revocation.
                goto_settled(a, base_url + "/auth/logout/")
                expect(a.locator("#id_username")).to_be_visible()
        finally:
            for refetch in refetches:
                refetch.continue_()
            b.unroute(refetch_url, defer_refetch)
    # Role revocation ends every old session, including the remaining clinic
    # membership's session. The final document must contain no clinical UI.
    expect(b).to_have_url(base_url + "/auth/login/")
    expect(b.locator("#id_username")).to_be_visible()
    expect(
        b.locator("#agenda-shell, .agenda-row, [data-realtime-topic]")
    ).to_have_count(0)
    expect(b.locator("body")).not_to_contain_text("Sintetico Realtime")
    post_revocation = tap.post_revocation()
    # A single grant followed by a refusal is still a grant (gate-review B1).
    assert [status for _method, status in post_revocation[:1]] == [403], post_revocation
    assert tap.grants() == [], post_revocation
    assert tap.streams, "the tap saw no realtime stream open"
    assert tap.open_streams == [], "b's realtime stream never closed"
    return post_revocation


RT_TELECONSULT_JS = (
    "document.addEventListener('rt:teleconsult', () => {"
    " window.__rtTeleconsult = (window.__rtTeleconsult || 0) + 1;"
    " window.__rtTeleconsultAt = Date.now(); }, true);"
)
HINT_BUDGET_MS = 2000


def _hinted(page: Page, started: float, before: int) -> float:
    """Return the hint latency once a new rt:teleconsult arrived on ``page``."""
    wait_for_js(page, f"() => (window.__rtTeleconsult || 0) > {before}")
    latency = float(page.evaluate("() => window.__rtTeleconsultAt")) - started
    assert latency <= HINT_BUDGET_MS, latency
    return latency


def _watch(page: Page, errors: list[str], ticket_refusals: list[str]) -> None:
    """Fail on any page or console error except the agenda's ticket refusal.

    The physician's agenda (appointment.read_own only) is refused a
    clinic-wide ticket by todo 8's design; that exact refusal is recorded.
    """
    page.set_default_timeout(20_000)
    watch_page_errors(page, errors)
    page.add_init_script(RT_TELECONSULT_JS)

    def console(message: ConsoleMessage) -> None:
        if message.type != "error":
            return
        if message.location.get("url", "").endswith("/rt/stream") and (
            "/agenda/" in page.url
        ):
            ticket_refusals.append(page.url)
        else:
            errors.append(message.text)

    page.on("console", console)


def _provision_settled(physician: Page, case: _Case, data: dict[str, str]) -> str:
    """Open the encounter from the agenda only after its realtime denial settles.

    A physician holds appointment.read_own, so the agenda's clinic ticket is
    refused and realtime.js reconciles with one htmx refetch; clicking before
    that refetch settles would abort it (WebKit logs htmx:sendError).
    """
    register_physician(case.staff)
    goto_settled(
        physician,
        f"{case.base}/scheduling/clinics/{case.staff['clinic_a']}/agenda/day/"
        f"{case.day}/1/",
    )
    expect(physician.locator("#agenda-shell")).to_have_attribute(
        "data-realtime-state", "denied"
    )
    wait_for_js(physician, SETTLED_JS)
    with expect_document(physician):
        physician.locator(
            f'form:has(input[name="appointment_id"][value="{data["appointment"]}"]) '
            'button[value="open"]'
        ).click()
    goto_settled(physician, case.staff_url)
    session_id = _create_session(physician, case.staff_url)
    _worker(_room_operation(case.staff, session_id), "sent", case.root)
    return session_id


def _count(page: Page) -> int:
    return int(page.evaluate("() => window.__rtTeleconsult || 0"))


def _now(page: Page) -> float:
    return float(page.evaluate("() => Date.now()"))


def test_teleconsult_hints_refetch_both_participants(
    renewal_base_url: str,
    renewal_artifact_root: Path,
    agenda_staff: dict[str, str],
) -> None:
    """Todo 37: room state reaches both participants from hints, not timers.

    Runs before the agenda scene, which ends by stopping the realtime process.
    """
    base = renewal_base_url
    case = _Case(
        base=base,
        staff=agenda_staff,
        day=TELECONSULT_DAY,
        root=renewal_artifact_root / "realtime-teleconsult",
        width=1280,
        staff_url=f"{base}/teleconsult/clinics/{agenda_staff['clinic_a']}/",
        patient_url=f"{base}/patient/teleconsult/",
    )
    case.root.mkdir(mode=0o700)
    errors: list[str] = []
    ticket_refusals: list[str] = []
    data = _seed_patient(case, 9, "Paciente Sintético Tempo Real")
    with sync_playwright() as driver:
        browser = launch_selected(driver, media=True)
        try:
            _hint_journey(browser, case, data, errors, ticket_refusals)
        finally:
            browser.close()


def _hint_journey(
    browser: Browser,
    case: _Case,
    data: dict[str, str],
    errors: list[str],
    ticket_refusals: list[str],
) -> None:
    base = case.base
    agenda_staff = case.staff
    contexts = [_context(browser, case, media=True) for _ in range(2)]
    contexts.append(_context(browser, case, media=False))
    try:
        physician, patient, admin = [context.new_page() for context in contexts]
        for page in (physician, patient, admin):
            _watch(page, errors, ticket_refusals)
        _sign_in_physician(physician, base, agenda_staff)
        sign_in_manager(admin, base, agenda_staff, seed_manager(agenda_staff))
        _publish_consent(admin, f"{base}/clinics/{agenda_staff['clinic_a']}/consent/")
        _redeem(patient, base, agenda_staff["clinic_a"], data["code"])
        _accept_consent(patient, base)
        session_id = _provision_settled(physician, case, data)
        _enter(physician, case, session_id, data["name"])
        workspace = physician.locator("[data-teleconsult='clinician']")
        expect(workspace).to_have_attribute("data-realtime-state", "connected")
        goto_settled(patient, case.patient_url)
        # The patient joins; the physician sees it with no click and no timer.
        before = _count(physician)
        started = _now(patient)
        with expect_document(patient):
            patient.locator('button[value="join"]').click()
        room = patient.locator("[data-teleconsult='room']")
        expect(room).to_have_attribute("data-realtime-state", "connected")
        joined = _hinted(physician, started, before)
        expect(physician.locator("[data-patient-presence]")).to_have_text("Na sala")
        # The patient's audio-only choice reaches the physician the same way.
        before = _count(physician)
        started = _now(patient)
        patient.locator("[data-audio-only]").click()
        audio = _hinted(physician, started, before)
        expect(physician.locator("[data-patient-media]")).to_have_text("Somente áudio")
        # Removal reaches the patient's room through the patient topic.
        before = _count(patient)
        started = _now(physician)
        _htmx(physician, 'button[value="remove"]')
        removed = _hinted(patient, started, before)
        expect(patient.locator("#room-panel")).to_have_attribute(
            "data-connection", "removed"
        )
        full_page_screenshot(physician, case.root / "physician-hinted.png")
        full_page_screenshot(patient, case.root / "patient-removed-hinted.png")
        assert not errors, errors
        (case.root / "hints.json").write_text(
            json.dumps(
                {
                    "latency_ms": {
                        "patient_joined": joined,
                        "patient_audio_only": audio,
                        "patient_removed": removed,
                    },
                    "budget_ms": HINT_BUDGET_MS,
                    "agenda_ticket_refusals": len(ticket_refusals),
                    "event_keys": ["kind", "topic_hash", "version"],
                },
                indent=2,
            )
            + "\n"
        )
    finally:
        for context in contexts:
            context.close()


GRANT_INBOX: Final = """
import sys
from uuid import UUID

import django

django.setup()
from apps.realtime.scopes import grant_clinic_topic
from apps.tenancy.db import tenant_context

actor, organization, clinic, user = map(UUID, sys.argv[1:5])
with tenant_context(actor, organization):
    print(grant_clinic_topic(
        clinic_id=clinic, user_id=user, kind="inbox", permission="appointment.read_own"
    ))
"""
# Installed before navigation. Counts realtime hints, and every worklist
# refresh clinical-worklist.js starts (its fetch of this path with HX-Request)
# and finishes: the returned promise's species wraps the worklist's own
# .finally callback, so a refresh is done only after it consumed the body,
# replaced or kept the board, cleared ``pending`` and started any coalesced
# rerun. Records no payload.
INBOX_HINTS_JS: Final = """(() => {
  for (const kind of ['inbox', 'poll']) {
    document.addEventListener('rt:' + kind, () => {
      window['rt_' + kind] = (window['rt_' + kind] || 0) + 1;
      window['rt_' + kind + '_at'] = performance.now();
    }, true);
  }
  window.worklistStarted = 0;
  window.worklistDone = 0;
  class Tracked extends Promise {
    finally(callback) {
      return super.finally(() => {
        try {
          return callback();
        } finally {
          window.worklistDone += 1;
        }
      });
    }
  }
  const native = window.fetch.bind(window);
  window.fetch = (input, init) => {
    const response = native(input, init);
    const headers = (init && init.headers) || {};
    if (input !== window.location.pathname || headers['HX-Request'] !== 'true') {
      return response;
    }
    window.worklistStarted += 1;
    return Tracked.resolve(response);
  };
})();"""
# The initial ready -> rt:poll reconciliation was consumed and nothing is in
# flight; worklistStarted >= 1 proves that request really started.
RECONCILED_JS: Final = (
    "() => (window.rt_poll || 0) >= 1 && window.worklistStarted >= 1"
    " && window.worklistDone === window.worklistStarted"
)
# Armed before the arrival. clinical-worklist.js refreshes on rt:inbox by
# fetching the page and calling #today-board.replaceWith(next); this observes
# that replacement and reads only the visit row's status attribute.
ARRIVAL_WITNESS_JS: Final = """([name, before]) => {
  const status = (board) => {
    const row = Array.from(board.querySelectorAll('li.today-appointment'))
      .find((item) => item.textContent.includes(name));
    return row ? row.getAttribute('data-today-status') : null;
  };
  window.todayReplacedScheduled = false;
  window.todayArrivedAt = 0;
  const observer = new MutationObserver((records) => {
    for (const record of records) {
      for (const node of record.removedNodes) {
        if (node.id === 'today-board' && status(node) === 'scheduled') {
          window.todayReplacedScheduled = true;
        }
      }
      for (const node of record.addedNodes) {
        if (
          node.id === 'today-board'
          && status(node) === 'arrived'
          && (window.rt_inbox || 0) > before
        ) {
          window.todayArrivedAt = performance.now();
          observer.disconnect();
          return;
        }
      }
    }
  });
  observer.observe(document.querySelector('[data-worklist="today"]'), {
    childList: true,
    subtree: true,
  });
}"""


# Fixed-offset IANA zones: at any UTC instant one of them is before local noon.
MORNING_ZONES: Final = ("Etc/UTC", "Etc/GMT+12")


def _today_scene(staff: dict[str, str]) -> dict[str, str]:
    """Seed an isolated clinic, clinician and receptionist for the Today scene.

    The database clock, which Today, reception and the appointment triggers
    all read, picks UTC before 12:00 UTC and UTC-12 otherwise. Both offsets
    are fixed (no DST), so the clinic-local time is in [00:00, 12:00) when the
    scene is seeded, at least 12 hours before the next local midnight; the
    runner bounds the suite far below that. Fresh identities in a fresh clinic
    keep availability and appointments disjoint from every other scene.
    """
    suffix = uuid4().hex[:8]
    scene = {
        **staff,
        "clinic_a": str(uuid4()),
        "physician_a_id": str(uuid4()),
        "physician_a": f"medica-hoje-{suffix}",
        "receptionist_id": str(uuid4()),
        "receptionist": f"recepcao-hoje-{suffix}",
        "totp_key": new_totp_key(),
    }
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant', %s, true)", [staff["organization"]]
        )
        zone = conn.execute(
            "SELECT CASE WHEN extract(hour FROM now() AT TIME ZONE 'UTC') < 12 "
            "THEN %s ELSE %s END",
            list(MORNING_ZONES),
        ).fetchone()
        assert zone is not None
        conn.execute(
            "INSERT INTO clinic_app.identity_clinic "
            "(id, organization_id, name, crm_uf, timezone) "
            "VALUES (%s, %s, 'Clínica Sintética Hoje', 'SP', %s)",
            [scene["clinic_a"], staff["organization"], zone[0]],
        )
        for user_id, username, role in (
            (scene["physician_a_id"], scene["physician_a"], "physician"),
            (scene["receptionist_id"], scene["receptionist"], "receptionist"),
        ):
            conn.execute(
                "INSERT INTO clinic_app.identity_user "
                "(id, username, password, email, first_name, last_name, is_active, "
                "is_staff, is_superuser, date_joined) "
                "VALUES (%s, %s, %s, %s, '', '', true, false, false, now())",
                [
                    user_id,
                    username,
                    make_password(staff["password"]),
                    f"{username}@hoje.invalid",
                ],
            )
            conn.execute(
                "INSERT INTO clinic_app.identity_userclinicrole "
                "(id, user_id, organization_id, clinic_id, role, "
                "workflow_bundle_version) VALUES (%s, %s, %s, %s, %s, 1)",
                [str(uuid4()), user_id, staff["organization"], scene["clinic_a"], role],
            )
        conn.execute("SET ROLE clinic_app")
        conn.execute(
            "SELECT set_config('app.current_user_id', %s, true)",
            [scene["physician_a_id"]],
        )
        conn.execute(
            "INSERT INTO clinic_app.otp_totp_totpdevice "
            "(name, confirmed, key, step, t0, digits, tolerance, drift, last_t, "
            "user_id, throttling_failure_count, created_at) "
            "VALUES ('Clinic OS authenticator', true, %s, 30, 0, 6, 1, 0, -1, "
            "%s, 0, now())",
            [scene["totp_key"], scene["physician_a_id"]],
        )
    return scene


def _grant_inbox(staff: dict[str, str], root: Path) -> None:
    """Opt the physician into the inbox topic through the real operator grant.

    An organization administrator holds ``staff.organization``; the lease is
    stored on this suite's own broker, which the served stream reads.
    """
    operator = _organization_admin(staff)["id"]
    env = {
        **os.environ,
        "APP_DATABASE_URL": worker_dsn(),
        "DJANGO_SETTINGS_MODULE": "config.settings.base",
        "CLINIC_DATA_MODE": "synthetic",
        "REALTIME_ENABLED": "true",
        "REALTIME_REDIS_URL": os.environ["CLINIC_RENEWAL_REALTIME_REDIS_URL"],
        "PYTHONPATH": str(Path.cwd()) + os.pathsep + str(Path.cwd() / "tests"),
    }
    args = (operator, staff["organization"], staff["clinic_a"], staff["physician_a_id"])
    result = subprocess.run(  # noqa: S603 - fixed test entrypoint and owned DSN
        [sys.executable, "-c", GRANT_INBOX, *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    (root / "grant-inbox.log").write_text(result.stdout + result.stderr)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"clinic:{staff['clinic_a']}:inbox"


def _seed_visit(staff: dict[str, str], name: str) -> str:
    """Seed a scheduled visit for the next database minute; return its local day.

    The start is in the future for the real future-start trigger, and
    ``_today_scene`` keeps it hours before clinic-local midnight.
    """
    enrollment = _seed_enrollment(staff, name)
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant', %s, true)", [staff["organization"]]
        )
        window = conn.execute(
            "SELECT date_trunc('minute', now()) + interval '1 minute', "
            "date_trunc('minute', now()) + interval '16 minutes'"
        ).fetchone()
        assert window is not None
        scope = [staff["organization"], staff["clinic_a"], staff["physician_a_id"]]
        conn.execute(
            "INSERT INTO clinic_app.scheduling_availabilityblock "
            "(id,organization_id,clinic_id,practitioner_id,start_at,end_at,"
            "idempotency_key,create_fingerprint,created_at,updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,now(),now())",
            [str(uuid4()), *scope, *window, str(uuid4()), secrets.token_bytes(32)],
        )
        conn.execute(
            "INSERT INTO clinic_app.scheduling_appointment "
            "(id,organization_id,clinic_id,patient_id,practitioner_id,start_at,end_at,"
            "idempotency_key,create_fingerprint,status,created_at,updated_at) "
            "SELECT %s,%s,%s,enrollment.patient_id,%s,%s,%s,%s,%s,'scheduled',"
            "now(),now() FROM clinic_app.intake_patientclinicenrollment enrollment "
            "WHERE enrollment.id=%s",
            [
                str(uuid4()),
                staff["organization"],
                staff["clinic_a"],
                staff["physician_a_id"],
                *window,
                str(uuid4()),
                secrets.token_bytes(32),
                enrollment,
            ],
        )
        day = conn.execute(
            "SELECT ((%s::timestamptz) AT TIME ZONE clinic.timezone)::date::text "
            "FROM clinic_app.identity_clinic clinic WHERE clinic.id=%s",
            [window[0], staff["clinic_a"]],
        ).fetchone()
    assert day is not None
    return str(day[0])


def test_arrival_refreshes_a_connected_clinician_today(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    agenda_staff: dict[str, str],
) -> None:
    # Runs before the revocation and process-loss scene, which is terminal.
    staff, base = _today_scene(agenda_staff), renewal_base_url
    root = renewal_artifact_root / "realtime-today"
    root.mkdir(mode=0o700)
    _grant_inbox(staff, root)
    name = "Sintetico Hoje Realtime"
    day = _seed_visit(staff, name)
    browser = renewal_page.context.browser
    assert browser is not None
    with browser.new_context(locale="pt-BR", service_workers="block") as reception:
        # Given: the clinician's Today holds a granted, connected inbox stream
        # and lists the scheduled visit after its own reconciliation settled.
        physician, receptionist = renewal_page, reception.new_page()
        receptionist.set_default_timeout(15000)
        physician.add_init_script(INBOX_HINTS_JS)
        _sign_in_physician(physician, base, staff)
        goto_settled(physician, f"{base}/clinics/{staff['clinic_a']}/today/")
        board = physician.locator('[data-worklist="today"]')
        expect(board).to_have_attribute("data-realtime-state", "connected")
        wait_for_js(physician, SETTLED_JS)
        wait_for_js(physician, RECONCILED_JS)
        visit = board.locator("li.today-appointment", has_text=name)
        expect(visit).to_have_attribute("data-today-status", "scheduled")
        _sign_in_receptionist(receptionist, base, staff)
        goto_settled(receptionist, base + _agenda_path(staff, "day", day))
        row = receptionist.locator(ROWS).filter(has_text=name)
        expect(row).to_have_attribute("data-appointment-status", "scheduled")
        before = physician.evaluate(
            "window.sameDocument = true; window.rtStarted = performance.now();"
            " window.rt_inbox || 0"
        )
        physician.evaluate(ARRIVAL_WITNESS_JS, [name, before])

        # When: reception records the patient's arrival.
        click_to_navigate(row.locator('button[name="action"][value="arrive"]'))
        expect(row).to_have_attribute("data-appointment-status", "arrived")

        # Then: that change's hint replaces the scheduled board with the arrived
        # one in place, after the hint and in the same document.
        wait_for_js(
            physician,
            f"() => (window.rt_inbox || 0) > {before}"
            " && window.rt_inbox_at >= window.rtStarted"
            " && window.todayReplacedScheduled === true"
            " && window.todayArrivedAt >= window.rt_inbox_at"
            " && window.worklistDone === window.worklistStarted",
        )
        expect(visit).to_have_attribute("data-today-status", "arrived")
        assert physician.evaluate("window.sameDocument === true")
        _accessibility(physician, root, 1280)


def test_two_sessions_refetch_and_fail_closed_without_realtime(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    agenda_staff: dict[str, str],
) -> None:
    root = renewal_artifact_root / "realtime"
    root.mkdir(mode=0o700)
    browser = renewal_page.context.browser
    assert browser is not None
    durations = []
    with (
        browser.new_context(
            locale="pt-BR", record_video_dir=root, service_workers="block"
        ) as first,
        browser.new_context(
            locale="pt-BR", record_video_dir=root, service_workers="block"
        ) as second,
    ):
        a, b = first.new_page(), second.new_page()
        a.set_default_timeout(15000)
        b.set_default_timeout(15000)
        _sign_in_receptionist(a, renewal_base_url, agenda_staff)
        _sign_in_receptionist(b, renewal_base_url, agenda_staff)
        # Record no tickets, cookie values, or URLs: only the invalidation keys.
        b.add_init_script(
            "document.addEventListener('rt:agenda', () => {"
            " window.rtReceived = performance.now(); }, true);"
        )
        for width, day in zip(WIDTHS, DAYS.values(), strict=True):
            _promise(
                agenda_staff, agenda_staff["physician_a_id"], (day, "08:00", "12:00")
            )
            name = f"Sintetico Realtime {width}"
            _seed_enrollment(agenda_staff, name)
            b.set_viewport_size({"width": width, "height": 900})
            url = renewal_base_url + _agenda_path(agenda_staff, "day", day)
            goto_settled(b, url)
            _connected(b)
            _open_booking(a, renewal_base_url, agenda_staff, name)
            _fill_booking(
                a, agenda_staff["physician_a"], f"{day}T09:00", f"{day}T09:30"
            )
            b.evaluate("window.rtStarted = performance.now()")
            with (
                b.expect_response(url) as refetch,
                expect_document(a),
            ):
                a.locator(BOOKING_SUBMIT).click()
            assert refetch.value.status == 200
            assert refetch.value.request.headers.get("hx-request") == "true"
            expect(b.locator(".agenda-row")).to_contain_text(name)
            duration = b.evaluate("performance.now() - window.rtStarted")
            durations.append(duration)
            assert duration <= 2000
            assert b.evaluate("window.rtReceived >= window.rtStarted")
            _accessibility(b, root, width)

        # Tap all of b's realtime traffic, then reload so the tap also sees
        # the stream under revocation open, not only close.
        tap = _RealtimeTap()
        b.on("request", tap.on_request)
        b.on("response", tap.on_response)
        b.on("requestfinished", tap.on_ended)
        b.on("requestfailed", tap.on_ended)
        goto_settled(b, b.url)

        _ticket_replay(b)
        _connected(b)

        post_revocation = _denied_after_revocation(
            a, b, renewal_base_url, agenda_staff, tap
        )
        api_status = b.evaluate(
            """async clinic => {
              const csrf = document.querySelector('[name=csrfmiddlewaretoken]').value;
              const response = await fetch('/api/ui/v1/agenda/query/', {
                method:'POST',
                headers:{'Content-Type':'application/json','X-CSRFToken':csrf},
                body:JSON.stringify({clinic_id:clinic,view:'day',
                  date:'2031-06-03',page:1})
              });
              return response.status;
            }""",
            agenda_staff["clinic_a"],
        )
        assert api_status == 403
        assert tap.grants() == [], tap.post_revocation()
        (root / "revoke.txt").write_text(
            "revoked session redirected to login; no agenda or clinical rows; "
            "stream closed; first post-revocation response 403; no "
            f"post-revocation 2xx {post_revocation}; authorized refetch API "
            "403; ticket replay 403\n"
        )

        # Clinic B membership remains, but the old sessions are both revoked.
        # A fresh login restores only the remaining scope before process loss.
        # Advance the browser clock, not wall time.
        _degraded(a, renewal_base_url, agenda_staff, root)
        video = a.video
        assert video is not None
    video.save_as(root / "realtime-2ctx.webm")
    (root / "latency.json").write_text(
        json.dumps(
            {
                "profile": "synthetic 3 viewport booking/refetch trials",
                "milliseconds": durations,
                "p95_ms": max(durations),
            }
        )
    )
