"""Synthetic platform passkey journeys; other engines prove the native fallback."""

from __future__ import annotations

import base64
from contextlib import contextmanager
from typing import TYPE_CHECKING

import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import load_der_private_key
from django.utils.translation import gettext
from playwright.sync_api import expect
from webauthn.helpers import bytes_to_base64url

from auth.passkey_test_support import AssertionOptions, SoftwareAuthenticator
from renewal.browser import _onboarding as journeys
from renewal.browser._navigation import goto_settled
from renewal.browser.test_retention import seed_manager, sign_in_manager

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from playwright.sync_api import Browser, CDPSession, Page

onboarding_staff = journeys.onboarding_staff


@contextmanager
def _recording(
    browser: Browser, root: Path, *, javascript: bool = True
) -> Iterator[Page]:
    """Keep one masked, named recording and delete the private raw copy."""
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    context = browser.new_context(
        locale="pt-BR",
        java_script_enabled=javascript,
        viewport={"width": 1280, "height": 850},
        service_workers="block",
        record_video_dir=str(root / "private-video"),
    )
    journeys.redact_video(context)
    page = context.new_page()
    video = page.video
    assert video is not None
    try:
        yield page
    finally:
        context.close()
        video.save_as(root / "passkey.webm")
        video.delete()
        (root / "private-video").rmdir()


def _virtual_authenticator(page: Page) -> tuple[CDPSession, str]:
    cdp = page.context.new_cdp_session(page)
    cdp.send("WebAuthn.enable", {"enableUI": False})
    result = cdp.send(
        "WebAuthn.addVirtualAuthenticator",
        {
            "options": {
                "protocol": "ctap2",
                "transport": "internal",
                "hasResidentKey": True,
                "hasUserVerification": True,
                "isUserVerified": True,
                "automaticPresenceSimulation": True,
            }
        },
    )
    return cdp, result["authenticatorId"]


def _organization_admin(staff: dict[str, str]) -> dict[str, str]:
    manager = seed_manager(staff)
    user_id = manager["id"]
    with psycopg.connect(staff["dsn"]) as connection:
        connection.execute(
            "SELECT set_config('app.current_tenant', %s, true)",
            [staff["organization"]],
        )
        connection.execute(
            "INSERT INTO clinic_app.identity_userclinicrole "
            "(id,user_id,organization_id,clinic_id,role,workflow_bundle_version) "
            "SELECT gen_random_uuid(),%s,%s,clinic.id,'org_admin',1 "
            "FROM clinic_app.identity_clinic clinic "
            "WHERE clinic.organization_id=%s AND NOT EXISTS ("
            "SELECT 1 FROM clinic_app.identity_userclinicrole membership "
            "WHERE membership.user_id=%s AND membership.clinic_id=clinic.id "
            "AND membership.role='org_admin' AND membership.revoked_at IS NULL)",
            [user_id, staff["organization"], staff["organization"], user_id],
        )
    return manager


def _register(page: Page, base: str) -> None:
    goto_settled(page, base + "/account/passkeys/")
    expect(page.locator("[data-passkey-start]")).to_be_visible()
    with page.expect_response(
        lambda response: response.url.endswith("/auth/passkey/register/finish/")
    ) as response:
        page.locator("[data-passkey-start]").click()
    assert response.value.status == 201
    expect(page.locator("[data-passkey-record]")).to_have_count(1)


def _opt_in(page: Page) -> None:
    page.locator("#id_enabled").check()
    assert (
        journeys.submit(
            page, 'form[action="/account/passkeys/policy/"] button[type=submit]'
        )
        == 200
    )


def _software_credential(
    cdp: CDPSession, authenticator_id: str
) -> tuple[SoftwareAuthenticator, str]:
    """Export only this synthetic device into ephemeral memory, never evidence."""
    device = cdp.send("WebAuthn.getCredentials", {"authenticatorId": authenticator_id})[
        "credentials"
    ][0]
    key = load_der_private_key(base64.b64decode(device["privateKey"]), password=None)
    assert isinstance(key, ec.EllipticCurvePrivateKey)
    software = SoftwareAuthenticator(key, base64.b64decode(device["credentialId"]))
    handle = bytes_to_base64url(base64.b64decode(device["userHandle"]))
    return software, handle


def test_platform_registration_step_up_and_sign_in(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_engine: str,
    renewal_artifact_root: Path,
    onboarding_staff: dict[str, str],
) -> None:
    browser = renewal_page.context.browser
    assert browser is not None
    manager = _organization_admin(onboarding_staff)
    root = renewal_artifact_root / "passkeys"
    with _recording(browser, root, javascript=renewal_engine == "chromium") as page:
        sign_in_manager(page, renewal_base_url, onboarding_staff, manager)
        if renewal_engine != "chromium":
            goto_settled(page, renewal_base_url + "/account/passkeys/")
            expect(page.locator("[data-passkey-start]")).to_be_hidden()
            expect(page.locator("[data-passkey-unavailable]")).to_be_visible()
            journeys.capture(page, root, f"{renewal_engine}-native-code-fallback")
            return
        cdp, authenticator_id = _virtual_authenticator(page)
        _register(page, renewal_base_url)
        _opt_in(page)
        for width in journeys.WIDTHS:
            page.set_viewport_size({"width": width, "height": 850})
            journeys.capture(page, root, f"happy-own-credentials-{width}")
        journeys.sign_out(page, renewal_base_url)
        goto_settled(page, renewal_base_url + "/auth/login/")
        page.locator("#id_username").fill(manager["username"])
        page.locator("#id_password").fill(onboarding_staff["password"])
        assert (
            journeys.submit(page, 'form[action="/auth/login/"] button[type=submit]')
            == 200
        )
        goto_settled(page, renewal_base_url + "/auth/step-up/?next=/account/passkeys/")
        expect(page.locator("#passkey-step-up [data-passkey-start]")).to_be_visible()
        with page.expect_response(
            lambda response: response.url.endswith("/auth/passkey/assert/finish/")
        ) as stepped_up:
            page.locator("#passkey-step-up [data-passkey-start]").click()
        assert stepped_up.value.status == 200
        expect(page.locator("[data-passkey-record]")).to_have_count(1)
        journeys.capture(page, root, "happy-platform-step-up")
        journeys.sign_out(page, renewal_base_url)
        goto_settled(page, renewal_base_url + "/auth/login/")
        with page.expect_response(
            lambda response: response.url.endswith("/auth/passkey/assert/finish/")
        ) as verified:
            page.locator("#passkey-login [data-passkey-start]").click()
        assert verified.value.status == 200
        expect(page).not_to_have_url(renewal_base_url + "/auth/login/")
        goto_settled(page, renewal_base_url + "/account/sessions/")
        expect(page.locator("[data-session-record]")).not_to_have_count(0)
        journeys.capture(page, root, "happy-passkey-session-inventory")
        cdp.send(
            "WebAuthn.removeVirtualAuthenticator", {"authenticatorId": authenticator_id}
        )
        cdp.detach()


@pytest.mark.parametrize("case", ["counter_regression", "foreign_rp"])
def test_signed_assertion_refusals(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    onboarding_staff: dict[str, str],
    case: str,
) -> None:
    browser = renewal_page.context.browser
    assert browser is not None
    root = renewal_artifact_root / f"passkey-{case}"
    engine = browser.browser_type.name
    manager = _organization_admin(onboarding_staff)
    with _recording(browser, root) as page:
        sign_in_manager(page, renewal_base_url, onboarding_staff, manager)
        if engine != "chromium":
            goto_settled(page, renewal_base_url + "/account/passkeys/")
            journeys.capture(page, root, f"{engine}-native-factor")
            return
        cdp, authenticator_id = _virtual_authenticator(page)
        _register(page, renewal_base_url)
        _opt_in(page)
        journeys.sign_out(page, renewal_base_url)
        goto_settled(page, renewal_base_url + "/auth/login/")
        with page.expect_response(
            lambda response: response.url.endswith("/auth/passkey/assert/finish/")
        ) as accepted:
            page.locator("#passkey-login [data-passkey-start]").click()
        assert accepted.value.status == 200
        expect(page).not_to_have_url(renewal_base_url + "/auth/login/")
        journeys.sign_out(page, renewal_base_url)
        goto_settled(page, renewal_base_url + "/auth/login/")
        if case == "foreign_rp":
            software, handle = _software_credential(cdp, authenticator_id)
            csrf = page.locator(
                '#passkey-login input[name="csrfmiddlewaretoken"]'
            ).input_value()
            begin = page.request.post(
                renewal_base_url + "/auth/passkey/assert/begin/",
                form={"csrfmiddlewaretoken": csrf, "purpose": "login"},
            )
            assert begin.status == 200
            denied = page.request.post(
                renewal_base_url + "/auth/passkey/assert/finish/",
                form={
                    "csrfmiddlewaretoken": csrf,
                    "credential": software.assertion(
                        challenge=begin.json()["challenge"],
                        user_handle=handle,
                        options=AssertionOptions(
                            rp_id="foreign.example", origin=renewal_base_url
                        ),
                    ),
                },
            )
            assert denied.status == 403
        else:
            device = cdp.send(
                "WebAuthn.getCredentials", {"authenticatorId": authenticator_id}
            )["credentials"][0]
            cdp.send(
                "WebAuthn.removeCredential",
                {
                    "authenticatorId": authenticator_id,
                    "credentialId": device["credentialId"],
                },
            )
            device["signCount"] -= 1
            cdp.send(
                "WebAuthn.addCredential",
                {"authenticatorId": authenticator_id, "credential": device},
            )
            with page.expect_response(
                lambda response: response.url.endswith("/auth/passkey/assert/finish/")
            ) as regression:
                page.locator("#passkey-login [data-passkey-start]").click()
            assert regression.value.status == 403
            expect(page.locator("[data-passkey-status]")).to_have_text(
                gettext(
                    "We could not verify the passkey. "
                    "Try again or use your authenticator code."
                )
            )
            with psycopg.connect(onboarding_staff["dsn"]) as connection:
                connection.execute(
                    "SELECT set_config('app.current_tenant', %s, true)",
                    [onboarding_staff["organization"]],
                )
                assert connection.execute(
                    "SELECT count(*) FROM clinic_app.audit_event event "
                    "JOIN clinic_app.identity_passkey credential "
                    "ON credential.id=event.affected_record_id "
                    "WHERE credential.user_id=%s "
                    "AND event.event_type='identity.passkey.counter_regression'",
                    [manager["id"]],
                ).fetchone() == (1,)
        journeys.capture(page, root, f"refused-{case}")
        cdp.send(
            "WebAuthn.removeVirtualAuthenticator", {"authenticatorId": authenticator_id}
        )
        cdp.detach()


def test_cancel_retry_and_offline_identity_feedback(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_engine: str,
    renewal_artifact_root: Path,
) -> None:
    browser = renewal_page.context.browser
    assert browser is not None
    root = renewal_artifact_root / "passkey-errors"
    with _recording(browser, root) as page:
        context = page.context
        goto_settled(page, renewal_base_url + "/auth/login/")
        if renewal_engine != "chromium":
            expect(
                page.locator('form[action="/auth/login/"] button[type=submit]')
            ).to_be_visible()
            journeys.capture(page, root, f"{renewal_engine}-password-fallback")
            return
        cdp, authenticator_id = _virtual_authenticator(page)
        cdp.send(
            "WebAuthn.setAutomaticPresenceSimulation",
            {
                "authenticatorId": authenticator_id,
                "enabled": False,
            },
        )
        page.locator("#passkey-login [data-passkey-start]").click()
        cancel = page.locator("#passkey-login [data-passkey-cancel]")
        expect(cancel).to_be_visible()
        cancel.click()
        status = page.locator("#passkey-login [data-passkey-status]")
        expect(status).to_have_text(
            gettext(
                "Passkey verification cancelled. "
                "Try again or use your authenticator code."
            )
        )
        journeys.capture(page, root, "failure-cancelled")
        context.set_offline(True)
        page.locator("#passkey-login [data-passkey-start]").click()
        expect(status).to_have_text(
            gettext("You are offline. Reconnect before verifying your identity.")
        )
        journeys.capture(page, root, "failure-offline")
        context.set_offline(False)
        expect(page.locator("#passkey-login [data-passkey-start]")).to_be_enabled()
        cdp.send(
            "WebAuthn.removeVirtualAuthenticator", {"authenticatorId": authenticator_id}
        )
        cdp.detach()
