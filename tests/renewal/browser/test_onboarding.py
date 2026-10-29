"""Todo 15 native onboarding, invitation refusal, and responsive security UI."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from playwright.sync_api import expect

from renewal.browser._fixture_secrets import FixtureSecret, new_password
from renewal.browser._navigation import goto_settled
from renewal.browser._onboarding import (
    WIDTHS,
    book_first,
    capture,
    confirm_enrollment,
    enrollment_fields,
    historical_invitation,
    invitation_details,
    invitation_refused,
    invite,
    onboarding_staff,
    redact_video,
    sign_out,
    submit,
    targets,
)
from renewal.browser._page_wait import evaluate_js
from renewal.browser.test_retention import seed_manager, sign_in_manager

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import Page

__all__ = ("onboarding_staff",)


@pytest.mark.parametrize("width", WIDTHS)
def test_invite_accept_enroll_and_book(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    onboarding_staff: dict[str, str],
    width: int,
) -> None:
    # Given a clinic manager and an opening agenda with no appointments.
    staff = onboarding_staff
    manager = seed_manager(staff)
    browser = renewal_page.context.browser
    assert browser is not None
    root = renewal_artifact_root / "onboarding" / str(width)
    root.mkdir(parents=True, mode=0o700)
    context = browser.new_context(
        locale="pt-BR",
        timezone_id="Asia/Tokyo",
        java_script_enabled=False,
        viewport={"width": width, "height": 900},
        service_workers="block",
        record_video_dir=str(root / "private-video"),
        record_video_size={"width": width, "height": 900},
    )
    redact_video(context)
    page = context.new_page()
    page.set_default_timeout(20_000)
    video = page.video
    assert video is not None
    try:
        sign_in_manager(page, renewal_base_url, staff, manager)
        staff_url = f"{renewal_base_url}/clinics/{staff['clinic_a']}/settings/staff/"
        onboarding = (
            f"{renewal_base_url}/clinics/{staff['clinic_a']}/settings/onboarding/"
        )
        goto_settled(page, onboarding)
        targets(page, "#main-content a")
        capture(page, root, "first-steps")
        goto_settled(page, staff_url)
        targets(page, "#main-content .actions > a")
        invitation_id = invite(page, f"reception-{uuid4().hex}@example.invalid")
        token, user_id = invitation_details(staff, invitation_id)
        capture(page, root, "invitation-queued")
        sign_out(page, renewal_base_url)
        goto_settled(page, renewal_base_url + "/auth/invitation/")
        username = FixtureSecret(f"recepcao-{uuid4().hex}")
        password = new_password()

        # When the recipient confirms password and TOTP, then books the first slot.
        enrollment_fields(page, token, username, password)
        assert submit(page, "form button[type=submit]") == 200
        expect(page.locator('img[src^="data:"]')).to_have_count(1)
        capture(page, root, "pending-enrollment")
        enrollment_fields(page, token, username, password)
        confirm_enrollment(page, staff, user_id)
        book_first(page, renewal_base_url, staff, width)
        capture(page, root, "first-appointment")

        # Then an already accepted token cannot confer access again.
        sign_out(page, renewal_base_url)
        invitation_refused(page, renewal_base_url, token, (username, password))
        capture(page, root, "accepted-token-replay-denied")
        assert evaluate_js(
            page, "localStorage.length === 0 && sessionStorage.length === 0"
        )
    finally:
        context.close()
        video.save_as(root / "onboarding.webm")
        video.delete()
        (root / "private-video").rmdir()


@pytest.mark.parametrize("width", WIDTHS)
def test_expired_invitation_has_portuguese_reinvite_path(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    onboarding_staff: dict[str, str],
    width: int,
) -> None:
    # Given genuine historical expiry, not a modified timestamp or timer.
    staff = onboarding_staff
    manager = seed_manager(staff)
    email = f"expired-{uuid4().hex}@example.invalid"
    expired = historical_invitation(staff, manager["id"], email)
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
    root = renewal_artifact_root / "onboarding" / str(width)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        # When the expired code is submitted, the recipient gets the recovery path.
        credentials = (FixtureSecret(f"expired-{uuid4().hex}"), new_password())
        invitation_refused(page, renewal_base_url, expired, credentials)
        capture(page, root, "expired-invitation")
        sign_in_manager(page, renewal_base_url, staff, manager)
        goto_settled(
            page, f"{renewal_base_url}/clinics/{staff['clinic_a']}/settings/staff/"
        )
        replacement_id = invite(page, email)
        replacement, _user_id = invitation_details(staff, replacement_id)
        assert replacement != expired
        sign_out(page, renewal_base_url)
        goto_settled(page, renewal_base_url + "/auth/invitation/")
        enrollment_fields(page, replacement, *credentials)
        # Then a real new invitation starts enrollment; the stale token still cannot.
        assert submit(page, "form button[type=submit]") == 200
        expect(page.locator('img[src^="data:"]')).to_have_count(1)
        capture(page, root, "reinvited-enrollment")
        invitation_refused(page, renewal_base_url, expired, credentials)
    finally:
        context.close()
