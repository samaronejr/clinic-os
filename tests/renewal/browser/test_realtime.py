"""Two-context real SSE, forced process loss, replay, revocation and a11y."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import psycopg
from django.utils.translation import gettext
from playwright.sync_api import expect

from renewal.browser._page_wait import wait_for_js
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
from renewal.browser.test_primitives import AXE_RUN_JS

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import Page

__all__ = ("agenda_staff",)
WIDTHS = (1280, 375, 320)


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
    page.screenshot(path=str(root / f"agenda-{width}.png"), full_page=True)


def _ticket_replay(page: Page) -> None:
    # Keep the credential in page memory; never in reports or logs.
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
      const stream = await fetch(endpoint, {signal:controller.signal});
      await stream.body.getReader().read();
      const replay = await fetch(endpoint);
      controller.abort();
      return [stream.status, replay.status];
    }""")
    assert status == [200, 403]


def _degraded(page: Page, base_url: str, clinic_id: str, root: Path) -> None:
    page.goto(base_url + f"/scheduling/clinics/{clinic_id}/agenda/")
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
    page.screenshot(path=str(root / "degraded.png"), full_page=True)


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
            b.goto(url)
            _connected(b)
            _open_booking(a, renewal_base_url, agenda_staff, name)
            _fill_booking(
                a, agenda_staff["physician_a"], f"{day}T09:00", f"{day}T09:30"
            )
            b.evaluate("window.rtStarted = performance.now()")
            with (
                b.expect_response(url) as refetch,
                a.expect_navigation(),
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

        _ticket_replay(b)

        # Owner SQL fixture revokes the clinic assignment. Logout is the real
        # immediate control event; unit tests independently prove role-delete
        # signals and periodic reauth when an event is lost.
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
        a.goto(renewal_base_url + "/auth/logout/")
        with (
            b.expect_response(
                lambda response: (
                    response.request.method == "POST"
                    and response.url.endswith("/rt/stream")
                )
            ) as denied,
            a.expect_navigation(),
        ):
            a.locator("form button[type=submit]").click()
        assert denied.value.status == 403
        expect(b.locator("#agenda-shell")).to_have_attribute(
            "data-realtime-state", "denied"
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
        (root / "revoke.txt").write_text(
            "stream closed; new ticket 403; authorized refetch API 403; "
            "ticket replay 403\n"
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
