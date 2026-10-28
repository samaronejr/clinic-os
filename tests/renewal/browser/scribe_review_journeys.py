"""Todo 42 journeys collected by the unchanged scribe-capture runner suite."""

from __future__ import annotations

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from django.utils.translation import gettext as _
from django_otp.oath import TOTP
from ops.testing.process_helpers import run_process
from playwright.sync_api import expect

from renewal.browser._navigation import (
    click_to_navigate,
    expect_document,
    goto_settled,
    reload_settled,
)
from renewal.browser.a11y_support import check_page
from renewal.browser.engines import full_page_screenshot
from renewal.browser.test_amendments import swap_totp_device
from renewal.browser.test_scribe_capture import open_capture

if TYPE_CHECKING:
    from playwright.sync_api import Page

MANUAL = "SINTETICO-42: edicao manual que deve permanecer."


def worker(staff: dict[str, str], mode: str, identifier: str) -> dict[str, str | int]:
    """Only an isolated fixture DSN reaches the subprocess's private environment."""
    arguments = (
        sys.executable,
        "-c",
        "import django; django.setup(); "
        "from renewal.scribe_review_fixture import main; main()",
        mode,
        staff["physician_a_id"],
        staff["clinic_a"],
        staff["organization"],
        identifier,
    )
    with pytest.MonkeyPatch.context() as patch:
        for name, value in {
            "APP_DATABASE_URL": staff["dsn"],
            "DJANGO_SETTINGS_MODULE": "config.settings.base",
            "CLINIC_DATA_MODE": "synthetic",
            "SCRIBE_SYNTHETIC_CAPTURE": "1",
            "SCRIBE_MEDIA_ROOT": str(
                Path(os.environ["CLINIC_SECRET_DIR"]) / "review-media"
            ),
            "CELERY_BROKER_URL": "memory://",
            "PYTHONPATH": os.pathsep.join((".", "tests")),
        }.items():
            patch.setenv(name, value)
        with ThreadPoolExecutor(max_workers=1) as process:
            result = process.submit(run_process, arguments, timeout_seconds=60).result()
    assert result.returncode == 0, result.stderr
    data: dict[str, str | int] = json.loads(result.stdout)
    return data


def open_review(page: Page, base: str, staff: dict[str, str], day: str) -> str:
    """Open the real encounter and its native POST review, with no URL selectors."""
    data = open_capture(page, base, staff, day)
    result = worker(staff, "seed", data["appointment"])
    reload_settled(page)
    click_to_navigate(page.locator('form[action$="/scribe/review/"] button'))
    expect(page.locator("[data-note-review]")).to_be_visible()
    expect(page.locator("[data-review-unavailable]")).to_have_count(0)
    return str(result["version"])


def generate(page: Page, staff: dict[str, str], version: str) -> None:
    click_to_navigate(page.locator('button[value="generate"]'))
    expect(page.locator("[data-review-queued]")).to_be_visible()
    assert worker(staff, "execute", version)["inference"] == "succeeded"
    click_to_navigate(page.locator('button[value="refresh"]'))
    expect(page.locator('[data-suggestion-state="pending"]')).to_have_count(3)


def capture_review(page: Page, base: str, root: Path, name: str) -> None:
    """Every review scene is checked at desktop, mobile and 400% reflow widths."""
    for width in (1280, 375, 320):
        page.set_viewport_size({"width": width, "height": 1000})
        assert page.evaluate(
            "() => document.documentElement.scrollWidth <= window.innerWidth"
        )
        check_page(page, base, root, f"review-{name}-{width}")
        full_page_screenshot(page, root / f"review-{name}-{width}.png")
    page.set_viewport_size({"width": 1280, "height": 1000})


def happy_review(page: Page, base: str, root: Path, staff: dict[str, str]) -> None:
    """Accept three sections by keyboard, then finalize through real step-up."""
    version = open_review(page, base, staff, "2042-06-02")
    generate(page, staff, version)
    capture_review(page, base, root, "pending")
    expect(page.locator("[data-separate-proposals]")).to_be_visible()
    page.locator("[data-suggestion] details").first.locator("summary").click()
    expect(page.locator("[data-suggestion] details").first).to_have_attribute(
        "open", ""
    )
    for _index in range(3):
        suggestion = page.locator('[data-suggestion-state="pending"]').first
        suggestion.focus()
        with expect_document(page):
            page.keyboard.press("Alt+Shift+A")
    assert worker(staff, "inspect", version)["decisions"] == 3
    expect(page.locator("[data-ai-use-record]")).to_have_count(3)
    page.locator("#id_objective").fill("Exame sintetico documentado pelo medico.")
    click_to_navigate(page.locator('button[value="save"]'))
    capture_review(page, base, root, "accepted")
    click_to_navigate(page.locator('a[href$="/encounter/"]'))
    key = swap_totp_device(staff)
    staff["totp_key"] = key
    click_to_navigate(page.locator('#finalize-form button[value="finalize"]'))
    assert "/auth/verify/" in page.url
    assert worker(staff, "inspect", version)["state"] == "draft"
    token = TOTP(bytes.fromhex(key), 30, 0, 6, 0).token()
    page.locator("#id_otp_token").fill(f"{token:06d}")
    click_to_navigate(page.locator("button[type=submit]"))
    expect(page.locator("#finalize-form")).to_be_visible()
    click_to_navigate(page.locator('#finalize-form button[value="finalize"]'))
    expect(page.locator("[data-finalization]")).to_have_attribute(
        "data-finalization", "local"
    )
    expect(page.locator("[data-ai-use-record]")).to_have_count(3)
    assert worker(staff, "inspect", version)["state"] == "finalized"
    capture_review(page, base, root, "finalized")


def late_review(page: Page, base: str, root: Path, staff: dict[str, str]) -> None:
    """A late inference cannot replace a saved or locally edited manual section."""
    version = open_review(page, base, staff, "2042-06-03")
    click_to_navigate(page.locator('button[value="generate"]'))
    page.locator("#id_subjective").fill(MANUAL)
    click_to_navigate(page.locator('button[value="refresh"]'))
    expect(page.locator("#id_subjective")).to_have_value(MANUAL)
    expect(page.locator("[data-review-note-state]")).to_have_attribute(
        "data-review-note-state", "unsaved"
    )
    click_to_navigate(page.locator('button[value="save"]'))
    assert worker(staff, "execute", version)["inference"] == "succeeded"
    click_to_navigate(page.locator('button[value="refresh"]'))
    expect(page.locator("#id_subjective")).to_have_value(MANUAL)
    conflict = page.locator('[data-merge-state="conflict"]')
    expect(conflict).to_have_count(1)
    expect(conflict.locator("[data-review-accept]")).to_be_disabled()
    expect(conflict.locator("textarea")).to_have_value(MANUAL)
    capture_review(page, base, root, "late-conflict")
    conflict.locator("textarea").fill("SINTETICO-42: substituicao explicita.")
    click_to_navigate(conflict.locator('button[value^="modified:"]'))
    expect(page.locator("#id_subjective")).to_have_value(
        "SINTETICO-42: substituicao explicita."
    )
    expect(page.locator('[data-suggestion-state="modified"]')).to_have_count(1)
    rejected = page.locator('[data-suggestion-state="pending"]').first
    rejected.focus()
    with expect_document(page):
        page.keyboard.press("Alt+Shift+R")
    expect(page.locator('[data-suggestion-state="rejected"]')).to_have_count(1)
    regenerated = page.locator('[data-suggestion-state="pending"]')
    expect(regenerated).to_have_count(1)
    click_to_navigate(regenerated.locator('button[value^="regenerate_"]'))
    assert worker(staff, "execute", version)["inference"] == "succeeded"
    click_to_navigate(page.locator('button[value="refresh"]'))
    expect(page.locator('[data-suggestion-state="pending"]')).to_have_count(1)
    assert worker(staff, "inspect", version)["decisions"] == 2


def native_review(page: Page, base: str, root: Path, staff: dict[str, str]) -> None:
    """No-JavaScript review, offline refusal and browser-storage privacy."""
    version = open_review(page, base, staff, "2042-06-04")
    generate(page, staff, version)
    page.context.set_offline(True)
    expect(page.locator("[data-review-offline]")).to_be_visible()
    expect(page.locator('button[value="save"]')).to_be_disabled()
    expect(page.locator("[data-review-accept]").first).to_be_disabled()
    page.context.set_offline(False)
    expect(page.locator("[data-review-offline]")).to_be_hidden()
    # HTMX stores only the current nonclinical route even without history HTML.
    # Require that exact singleton; no note, transcript or history cache may exist.
    assert page.evaluate(
        """() => ({
          local: Object.fromEntries(Object.entries(localStorage)),
          session: Object.fromEntries(Object.entries(sessionStorage))
        })"""
    ) == {
        "local": {},
        "session": {
            "htmx-current-path-for-history": (
                f"/clinics/{staff['clinic_a']}/scribe/review/"
            )
        },
    }
    assert page.evaluate("async () => (await indexedDB.databases()).length") == 0
    assert page.evaluate(
        """async () => (await Promise.all((await caches.keys()).map(
          async key => (await (await caches.open(key)).keys()).every(
            request => new URL(request.url).pathname.startsWith('/static/'))
        ))).every(Boolean)"""
    )
    browser = page.context.browser
    assert browser is not None
    context = browser.new_context(
        java_script_enabled=False,
        locale="pt-BR",
        storage_state=page.context.storage_state(),
        viewport={"width": 375, "height": 1000},
    )
    try:
        native = context.new_page()
        goto_settled(native, f"{base}/ehr/clinics/{staff['clinic_a']}/encounter/")
        click_to_navigate(native.locator('form[action$="/scribe/review/"] button'))
        native.locator("#id_subjective").fill(MANUAL)
        click_to_navigate(native.locator('button[value="save"]'))
        expect(native.locator("#id_subjective")).to_have_value(MANUAL)
        expect(native.locator("[data-review-saved]")).to_be_visible()
        full_page_screenshot(native, root / "review-native-375.png")
    finally:
        context.close()
    assert worker(staff, "inspect", version)["revision"] == 2


def foreign_review(page: Page, base: str, root: Path, staff: dict[str, str]) -> None:
    """A foreign model citation reaches validation but never reaches the note."""
    version = open_review(page, base, staff, "2042-06-05")
    click_to_navigate(page.locator('button[value="generate"]'))
    rejected = worker(staff, "foreign", version)
    assert rejected["inference"] == "failed"
    assert rejected["suggestions"] == 0
    assert rejected["revision"] == 1
    click_to_navigate(page.locator('button[value="refresh"]'))
    expect(page.locator("[data-suggestion]")).to_have_count(0)
    expect(page.locator("#id_subjective")).to_have_value("")
    expect(
        page.get_by_text(
            _(
                "No supported suggestions are available. "
                "Nothing has been added to the note."
            ),
            exact=True,
        )
    ).to_be_visible()
    capture_review(page, base, root, "foreign-rejected")
