"""Responsive private recovery, session replay, and localized signup errors."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from django.utils.translation import gettext
from playwright.sync_api import expect

from renewal.browser._fixture_secrets import FixtureSecret, new_password
from renewal.browser._navigation import goto_settled
from renewal.browser._onboarding import (
    WIDTHS,
    capture,
    onboarding_staff,
    redact_video,
    submit,
    targets,
)
from renewal.browser._page_wait import await_autofocus, evaluate_js
from renewal.browser.test_retention import seed_manager, sign_in_manager

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import Page

__all__ = ("onboarding_staff",)


@pytest.mark.parametrize("width", WIDTHS)
def test_recovery_reflow_once_only_and_next_request_revocation(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    onboarding_staff: dict[str, str],
    width: int,
) -> None:
    # Given a real newly verified manager session.
    staff = onboarding_staff
    manager = seed_manager(staff)
    browser = renewal_page.context.browser
    assert browser is not None
    root = renewal_artifact_root / "onboarding" / str(width)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    context = browser.new_context(
        locale="pt-BR",
        java_script_enabled=False,
        viewport={"width": width, "height": 900},
        service_workers="block",
    )
    redact_video(context)
    page = context.new_page()
    page.set_default_timeout(20_000)
    try:
        sign_in_manager(page, renewal_base_url, staff, manager)
        recovery = f"{renewal_base_url}/clinics/{staff['clinic_a']}/account/recovery/"
        goto_settled(page, recovery)
        # When the user enrolls recovery and then revokes the current session.
        assert submit(page, 'form[action$="/recovery/enroll/"] button') == 200
        codes = page.locator("[data-private-recovery-codes] code")
        expect(codes).to_have_count(10)
        private_codes = [FixtureSecret(code) for code in codes.all_text_contents()]
        assert all(len(code) > 20 for code in private_codes)
        capture(page, root, "recovery-shown-once")
        assert evaluate_js(
            page, "localStorage.length === 0 && sessionStorage.length === 0"
        )
        assert submit(page, 'a[href$="/account/recovery/"]') == 200
        expect(page.locator("[data-private-recovery-codes]")).to_have_count(0)
        targets(page, "#main-content > section > p a")
        goto_settled(page, renewal_base_url + "/account/sessions/")
        old_cookies = context.cookies()
        assert submit(page, "[data-session-record] form button") == 200
        expect(page.locator("[data-session-revoked]")).to_be_visible()
        context.clear_cookies()
        context.add_cookies(
            [
                {
                    "name": cookie["name"],
                    "value": cookie["value"],
                    "domain": cookie["domain"],
                    "path": cookie["path"],
                    "expires": cookie["expires"],
                    "httpOnly": cookie["httpOnly"],
                    "secure": cookie["secure"],
                    "sameSite": cookie["sameSite"],
                }
                for cookie in old_cookies
            ]
        )
        # Then the first replay request redirects to login, not the workspace.
        with page.expect_response(
            lambda r: r.request.method == "GET" and r.url.endswith("/auth/protected/")
        ) as refused:
            goto_settled(page, renewal_base_url + "/auth/protected/")
        assert refused.value.status == 302
        assert refused.value.headers["location"].startswith("/auth/login/")
        expect(page.locator("#id_username")).to_be_visible()
        capture(page, root, "revoked-cookie-login")
        assert evaluate_js(
            page, "localStorage.length === 0 && sessionStorage.length === 0"
        )
    finally:
        context.close()


@pytest.mark.parametrize("width", WIDTHS)
def test_invalid_cnpj_error_and_return_target(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    width: int,
) -> None:
    # Given an otherwise complete anonymous registration form.
    browser = renewal_page.context.browser
    assert browser is not None
    context = browser.new_context(
        locale="pt-BR",
        java_script_enabled=False,
        viewport={"width": width, "height": 900},
        service_workers="block",
    )
    redact_video(context)
    page = context.new_page()
    page.set_default_timeout(20_000)
    try:
        goto_settled(page, renewal_base_url + "/auth/signup/")
        password = new_password()
        fields = {
            "organization_name": "Organizacao Sintetica",
            "legal_responsible": "Responsavel Sintetico",
            "username": FixtureSecret(f"signup-{uuid4().hex}"),
            "email": FixtureSecret(f"signup-{uuid4().hex}@example.invalid"),
            "password": password,
            "password_confirm": password,
            "cnpj": "12345678000196",
        }
        for name, value in fields.items():
            page.locator(f"#id_{name}").fill(value)
        # When a checksum-invalid CNPJ is posted, assert translated shipped copy.
        assert submit(page, "form button[type=submit]") == 400
        await_autofocus(page.locator("[data-focus-error]"))
        expect(page.locator("#id_cnpj_error .field-error")).to_have_text(
            gettext("Enter a valid CNPJ.")
        )
        assert gettext("Enter a valid CNPJ.") != "Enter a valid CNPJ."
        # Then the error reflows and the return link remains a 44px action.
        targets(page, '#main-content a[href="/auth/login/"]')
        root = renewal_artifact_root / "onboarding" / str(width)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        capture(page, root, "invalid-cnpj")
    finally:
        context.close()
