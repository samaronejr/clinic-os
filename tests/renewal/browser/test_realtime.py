"""Two-context real SSE, forced process loss, replay, revocation and a11y."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Final
from urllib.parse import urlparse

import psycopg
from django.utils.translation import gettext
from playwright.sync_api import expect, sync_playwright

from renewal.browser._navigation import expect_document, goto_settled
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
    from pathlib import Path

    from playwright.sync_api import Browser, ConsoleMessage, Page, Request, Response

__all__ = ("agenda_staff",)
WIDTHS = (1280, 375, 320)
TELECONSULT_DAY = "2035-08-12"
# Ticket POST and EventSource GET share this one same-origin path.
REALTIME_PATH: Final = "/rt/stream"


class _RealtimeTap:
    """Every /rt/stream request and response of one page, against a revocation.

    Boundary rule, causal and clock-free: ``mark_boundary`` runs BEFORE the
    owner transaction that deletes the role commits, and freezes the list of
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


def _degraded(page: Page, base_url: str, clinic_id: str, root: Path) -> None:
    goto_settled(page, base_url + f"/scheduling/clinics/{clinic_id}/agenda/")
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
    # Owner SQL fixture revokes the clinic assignment. Logout is the real
    # immediate control event; unit tests independently prove role-delete
    # signals and periodic reauth when an event is lost.
    # The refusal wait is armed before the revocation: b's stream can also
    # close for other reasons (a transport drop and its backoff retry, the
    # periodic reauthorization), and a refused request that lands before a
    # late wait exists is lost (hosted run 36475967514; fix-a16). It matches
    # only a post-revocation 403, so a late or absent refusal times out.
    with b.expect_response(tap.is_refusal):
        # The mark precedes the commit, so every server decision after the
        # commit is post-revocation (gate-review r2 B1: no clock boundary).
        tap.mark_boundary(b)
        with psycopg.connect(agenda_staff["dsn"]) as owner:
            owner.execute(
                "SELECT set_config('app.current_tenant', %s, true)",
                [agenda_staff["organization"]],
            )
            owner.execute(
                "DELETE FROM clinic_app.identity_userclinicrole "
                "WHERE clinic_id = %s AND user_id = %s",
                [agenda_staff["clinic_a"], agenda_staff["receptionist_id"]],
            )
        # Leaving the connection context committed the delete.
        goto_settled(a, base_url + "/auth/logout/")
        with expect_document(a):
            a.locator("form button[type=submit]").click()
    # The client closes its EventSource before every new ticket request, and
    # its denied state follows the refused response: once denied is visible,
    # every earlier realtime event has been dispatched to the tap.
    expect(b.locator("#agenda-shell")).to_have_attribute(
        "data-realtime-state", "denied"
    )
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
        browser.new_context(locale="pt-BR", record_video_dir=root) as first,
        browser.new_context(locale="pt-BR", record_video_dir=root) as second,
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
            "stream closed; first post-revocation response 403; no "
            f"post-revocation 2xx {post_revocation}; authorized refetch API "
            "403; ticket replay 403\n"
        )

        # Clinic B membership remains: ordinary work continues during an actual
        # stopped uvicorn process. Advance the browser clock, not wall time.
        _degraded(b, renewal_base_url, agenda_staff["clinic_b"], root)
        video = b.video
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
