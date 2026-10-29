"""Independent browser raters, delayed adjudication, HX parity and private storage."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from contextlib import ExitStack
from typing import TYPE_CHECKING, Final
from urllib.parse import urlsplit
from uuid import UUID

import psycopg
from playwright.sync_api import expect

from renewal.browser._fixture_secrets import OWNER_PASSWORD, FixtureSecret
from renewal.browser._navigation import (
    expect_document,
    goto_settled,
)
from renewal.browser._page_wait import _answer, evaluate_js, wait_for_js
from renewal.browser.a11y_support import AXE_URL, check_page
from renewal.browser.engines import full_page_screenshot
from renewal.browser.evaluation_accessibility import fill_rating, required_states
from renewal.browser.evaluation_storage import (
    observe_storage,
    register_storage_observer,
)
from renewal.browser.test_retention import sign_in_manager

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import Locator, Page, Route

OPEN = 'form.evaluation-command:has(input[name="action"][value="open"]) button'
RATING = "[data-evaluation-rating]"
DISAGREEMENT = "#evaluation-disagreement-title"
SLOTS = ("rater_one", "rater_two", "adjudicator")
SUBMIT_TIMEOUT_MS: Final = 20_000


def seed_review(staff: dict[str, str], theme: str) -> dict[str, str]:
    """Only fixture setup uses the owner subprocess; every review uses HTTP."""
    result = subprocess.run(  # noqa: S603 - fixed fixture and opaque scope IDs.
        [
            sys.executable,
            "-m",
            "renewal.evaluation_fixture",
            staff["organization"],
            staff["clinic_a"],
            theme,
        ],
        env={
            **os.environ,
            "APP_DATABASE_URL": staff["dsn"],
            "DJANGO_SETTINGS_MODULE": "config.settings.base",
            "CLINIC_DATA_MODE": "synthetic",
            OWNER_PASSWORD: staff["password"],
            "PYTHONPATH": ".:tests",
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert isinstance(data, dict)
    seeded = {str(key): str(value) for key, value in data.items()}
    assert str(UUID(seeded["run_id"])) == seeded["run_id"]
    for slot in SLOTS:
        seeded[f"{slot}_totp_key"] = FixtureSecret(seeded[f"{slot}_totp_key"])
    return seeded


def _hx_enter(page: Page) -> None:
    """Only the already-subscribed HX form uses Enter without a document."""
    _answer(page, page.keyboard._impl_obj.press("Enter"), SUBMIT_TIMEOUT_MS)


def submit(
    page: Page,
    button: Locator,
    *,
    enhanced: bool,
    invalid: bool = False,
    keyboard: bool = False,
) -> None:
    """Subscribe before pressing; distinguish an actual HX swap from navigation."""
    expected_status = 400 if invalid else 200
    url = page.url
    if enhanced:
        evaluate_js(
            page,
            """() => {
              window.__evaluationSettled = false;
              document.addEventListener('htmx:afterSettle', () => {
                window.__evaluationSettled = true;
              }, {once: true});
            }""",
            timeout=SUBMIT_TIMEOUT_MS,
        )
    with page.expect_response(
        lambda response: response.url == url and response.request.method == "POST",
        timeout=SUBMIT_TIMEOUT_MS,
    ) as response:
        button.focus(timeout=SUBMIT_TIMEOUT_MS)
        if enhanced:
            if keyboard:
                _hx_enter(page)
            else:
                button.click(timeout=SUBMIT_TIMEOUT_MS)
        else:
            with expect_document(
                page, wait_until="domcontentloaded", timeout=SUBMIT_TIMEOUT_MS
            ):
                _answer(
                    page,
                    page.keyboard._impl_obj.press("Enter"),
                    SUBMIT_TIMEOUT_MS,
                )
                assert response.value.status == expected_status, {
                    "expected_status": expected_status,
                    "observed_status": response.value.status,
                }
    assert response.value.status == expected_status, {
        "expected_status": expected_status,
        "observed_status": response.value.status,
    }
    assert (response.value.request.headers.get("hx-request") == "true") is enhanced
    directives = {
        value.strip() for value in response.value.headers["cache-control"].split(",")
    }
    assert directives == {"private", "no-store", "no-cache", "must-revalidate"}
    assert "'unsafe-eval'" not in response.value.headers["content-security-policy"]
    if enhanced:
        wait_for_js(
            page, "window.__evaluationSettled === true", timeout=SUBMIT_TIMEOUT_MS
        )


def open_review(page: Page, *, enhanced: bool) -> None:
    submit(page, page.locator(OPEN).first, enhanced=enhanced)
    expect(page.locator(".evaluation-source")).to_have_count(2)
    if enhanced:
        expect(page.locator("#evaluation-source-title")).to_be_focused()


def independent(page: Page, seeded: dict[str, str]) -> None:
    """The projection contains neither candidate identity nor another reviewer's ID."""
    expect(page.locator(DISAGREEMENT)).to_have_count(0)
    html = page.locator("#evaluation-workspace").inner_html()
    assert seeded["model_id"] not in html
    for slot in SLOTS:
        assert seeded[f"{slot}_id"] not in html
    expect(page.locator(f'{RATING} input[type="radio"]:checked')).to_have_count(0)


def audit_review(page: Page, base: str, root: Path, state: str) -> None:
    """Audit native HTTP output without enabling scripts in the functional context.

    A scripts-disabled browser suppresses axe's load event and timer callbacks.
    Replay the same native form state in a separate authenticated context that
    blocks every application script, but permits the actual vendored axe script.
    The functional context stays JavaScript-disabled throughout its journey.
    """
    if page.context._impl_obj._options.get("javaScriptEnabled") is not False:
        check_page(page, base, root, state)
        return
    browser = page.context.browser
    assert browser is not None
    blocked: list[str] = []

    def scripts(route: Route) -> None:
        path = urlsplit(route.request.url).path
        if route.request.resource_type == "script" and path != AXE_URL:
            blocked.append(path)
            route.abort()
        else:
            route.continue_()

    with browser.new_context(
        storage_state=page.context.storage_state(),
        viewport=page.viewport_size,
        locale="pt-BR",
        timezone_id="Asia/Tokyo",
        service_workers="block",
    ) as context:
        context.route("**/*", scripts)
        audit = context.new_page()
        audit.set_default_timeout(SUBMIT_TIMEOUT_MS)
        goto_settled(audit, page.url)
        open_review(audit, enhanced=False)
        if state.endswith("-required"):
            submit(
                audit, audit.locator(f"{RATING} button"), enhanced=False, invalid=True
            )
        assert (
            audit.locator("#evaluation-workspace").inner_text()
            == page.locator("#evaluation-workspace").inner_text()
        )
        assert blocked
        assert evaluate_js(audit, "typeof htmx") == "undefined"
        result = check_page(audit, base, root, state)
        result.update(
            {
                "functional_context_javascript_enabled": False,
                "audit_context_application_scripts_blocked": True,
                "native_http_visible_text_equal": True,
                "blocked_script_paths": sorted(set(blocked)),
            }
        )
        destination = root / "axe" / f"{state}-native-replay.json"
        destination.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")
        destination.chmod(0o600)


def rate(page: Page, *, enhanced: bool, negation: int = 0) -> list[str]:
    steps = fill_rating(page, negation=negation)
    submit(
        page,
        page.locator(f'{RATING} button[type="submit"]'),
        enhanced=enhanced,
        keyboard=True,
    )
    expect(page.locator("[data-evaluation-saved]")).to_be_visible()
    expect(page.locator(RATING)).to_have_count(0)
    return steps


def private_storage(page: Page, path: str) -> dict[str, object]:
    """Permit only HTMX's nonclinical route, no history HTML or clinical stores."""
    stored = evaluate_js(
        page,
        """() => ({
          local: Object.fromEntries(Object.entries(localStorage)),
          session: Object.fromEntries(Object.entries(sessionStorage))
        })""",
    )
    assert stored["local"] == {}
    assert stored["session"] in (
        {},
        {"htmx-current-path-for-history": path},
    )
    storage = observe_storage(page)
    assert evaluate_js(page, "document.documentElement.scrollWidth <= innerWidth")
    return storage


def assert_saved_votes(staff: dict[str, str], seeded: dict[str, str]) -> None:
    """Read each immutable rating as its real runtime actor, not as owner."""
    for slot in SLOTS:
        with psycopg.connect(staff["dsn"]) as connection:
            connection.execute("SET ROLE clinic_app")
            connection.execute(
                "SELECT set_config('app.current_tenant',%s,true)",
                [staff["organization"]],
            )
            connection.execute(
                "SELECT set_config('app.current_user_id',%s,true)",
                [seeded[f"{slot}_id"]],
            )
            assert connection.execute(
                "SELECT slot,labels->>'negation' "
                "FROM clinic_app.ai_evaluationrating "
                "WHERE run_id=%s AND user_id=%s",
                [seeded["run_id"], seeded[f"{slot}_id"]],
            ).fetchall() == [(slot, "1" if slot == "rater_two" else "0")]


def blinded_journey(
    page: Page,
    base: str,
    root: Path,
    staff: dict[str, str],
    *,
    presentation: tuple[int, str, bool],
) -> None:
    """Exercise both independent votes and the adjudicator at each presentation."""
    width, theme, enhanced = presentation
    seeded = seed_review(staff, theme)
    browser = register_storage_observer(page)
    path = f"/clinics/{staff['clinic_a']}/evaluation/"
    mode = "hx" if enhanced else "native"
    scene = f"evaluation-{mode}-{theme}-{width}"
    folder = root / "clinic-settings"
    folder.mkdir(exist_ok=True, mode=0o700)
    with ExitStack() as stack:
        reviewers = {}
        for slot in SLOTS:
            context = stack.enter_context(
                browser.new_context(
                    java_script_enabled=enhanced,
                    viewport={"width": width, "height": 900},
                    locale="pt-BR",
                    timezone_id="Asia/Tokyo",
                )
            )
            review = context.new_page()
            review.set_default_timeout(SUBMIT_TIMEOUT_MS)
            sign_in_manager(
                review,
                base,
                staff,
                {
                    key: seeded[f"{slot}_{key}"]
                    for key in ("id", "username", "totp_key")
                },
            )
            response = goto_settled(review, base + path)
            assert response is not None
            assert response.status == 200
            open_review(review, enhanced=enhanced)
            independent(review, seeded)
            reviewers[slot] = review
        first, second, adjudicator = (reviewers[slot] for slot in SLOTS)
        expect(adjudicator.locator(RATING)).to_have_count(0)
        submit(
            first, first.locator(f"{RATING} button"), enhanced=enhanced, invalid=True
        )
        full_page_screenshot(first, folder / f"{scene}-required.png")
        accessibility = required_states(first, folder, scene)
        audit_review(first, base, root, f"{scene}-required")
        keyboard = {"rater_one": rate(first, enhanced=enhanced)}
        open_review(adjudicator, enhanced=enhanced)
        independent(adjudicator, seeded)
        expect(adjudicator.locator(RATING)).to_have_count(0)
        open_review(second, enhanced=enhanced)
        independent(second, seeded)
        keyboard["rater_two"] = rate(second, enhanced=enhanced, negation=1)
        open_review(first, enhanced=enhanced)
        independent(first, seeded)
        open_review(adjudicator, enhanced=enhanced)
        expect(adjudicator.locator(DISAGREEMENT)).to_be_visible()
        expect(
            adjudicator.locator("section:has(> #evaluation-disagreement-title) dl")
        ).to_have_count(2)
        audit_review(adjudicator, base, root, f"{scene}-adjudication")
        full_page_screenshot(adjudicator, folder / f"{scene}-adjudication.png")
        keyboard["adjudicator"] = rate(adjudicator, enhanced=enhanced)
        storage = {}
        for slot, review in reviewers.items():
            assert review.url == base + path
            storage[slot] = private_storage(review, path)
            full_page_screenshot(review, folder / f"{scene}-{slot}-saved.png")
        assert_saved_votes(staff, seeded)
        destination = folder / f"{scene}-accessibility.json"
        destination.write_text(
            json.dumps(
                {
                    "required_state": accessibility,
                    "keyboard_rating_steps": keyboard,
                    "original_context_storage": storage,
                },
                sort_keys=True,
                indent=2,
            )
            + "\n"
        )
        destination.chmod(0o600)
