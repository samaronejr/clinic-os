"""Synthetic platform passkey journeys; other engines prove the native fallback."""

from __future__ import annotations

import base64
from contextlib import ExitStack, contextmanager
from typing import TYPE_CHECKING

import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from cryptography.hazmat.primitives.serialization import load_der_private_key
from django.utils.translation import gettext
from playwright.sync_api import expect
from webauthn.helpers import bytes_to_base64url
from webauthn.helpers.cose import COSEAlgorithmIdentifier

from auth.passkey_test_support import AssertionOptions, SoftwareAuthenticator
from renewal.browser import _onboarding as journeys
from renewal.browser._navigation import goto_settled
from renewal.browser._passkey_client import client_evidence
from renewal.browser.engines import (
    scroll_width_includes_flex_end_padding,
    virtual_authenticator,
)
from renewal.browser.test_primitives import OVERFLOW_JS
from renewal.browser.test_retention import seed_manager, sign_in_manager

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from playwright.sync_api import Browser, CDPSession, Page

onboarding_staff = journeys.onboarding_staff


@contextmanager
def _recording(
    browser: Browser,
    root: Path,
    *,
    javascript: bool = True,
    expected_denial: bool = False,
    expected_abort: bool = False,
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
    with client_evidence(
        page, root, expected_denial=expected_denial, expected_abort=expected_abort
    ):
        try:
            yield page
        finally:
            context.close()
            video.save_as(root / "passkey.webm")
            video.delete()
            (root / "private-video").rmdir()


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


def _register(page: Page, base: str) -> COSEAlgorithmIdentifier:
    goto_settled(page, base + "/account/passkeys/")
    expect(page.locator("[data-passkey-start]")).to_be_visible()
    with (
        page.expect_response(
            lambda response: response.url.endswith("/auth/passkey/register/finish/")
        ) as response,
        page.expect_console_message(
            lambda message: message.text.startswith("PASSKEY_ALGORITHM:")
        ) as algorithm,
    ):
        page.locator("[data-passkey-start]").click()
    assert response.value.status == 201
    expect(page.locator("[data-passkey-record]")).to_have_count(1)
    return COSEAlgorithmIdentifier(
        int(algorithm.value.text.removeprefix("PASSKEY_ALGORITHM:"))
    )


def _opt_in(page: Page) -> None:
    page.locator("#id_enabled").check()
    assert (
        journeys.submit(
            page, 'form[action="/account/passkeys/policy/"] button[type=submit]'
        )
        == 200
    )


def _software_credential(
    cdp: CDPSession,
    authenticator_id: str,
    *,
    algorithm: COSEAlgorithmIdentifier,
) -> tuple[SoftwareAuthenticator, str, int]:
    """Export only this synthetic device into ephemeral memory, never evidence."""
    device = cdp.send("WebAuthn.getCredentials", {"authenticatorId": authenticator_id})[
        "credentials"
    ][0]
    key = load_der_private_key(base64.b64decode(device["privateKey"]), password=None)
    assert isinstance(
        key, (ec.EllipticCurvePrivateKey, ed25519.Ed25519PrivateKey, rsa.RSAPrivateKey)
    )
    software = SoftwareAuthenticator(
        key, base64.b64decode(device["credentialId"]), algorithm
    )
    handle = bytes_to_base64url(base64.b64decode(device["userHandle"]))
    sign_count = device["signCount"]
    assert type(sign_count) is int
    return software, handle, sign_count + 1


def test_platform_registration_step_up_and_sign_in(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_engine: str,
    renewal_artifact_root: Path,
    onboarding_staff: dict[str, str],
) -> None:
    browser = renewal_page.context.browser
    assert browser is not None
    base = renewal_base_url.replace("127.0.0.1", "localhost", 1)
    manager = _organization_admin(onboarding_staff)
    root = renewal_artifact_root / "passkeys"
    with _recording(browser, root, javascript=renewal_engine == "chromium") as page:
        sign_in_manager(page, base, onboarding_staff, manager)
        goto_settled(page, base + "/account/passkeys/")
        for width in journeys.WIDTHS:
            page.set_viewport_size({"width": width, "height": 850})
            assert (
                page.locator("#main-content").evaluate(
                    OVERFLOW_JS, scroll_width_includes_flex_end_padding(page.context)
                )
                == []
            )
        if renewal_engine != "chromium":
            expect(page.locator("[data-passkey-start]")).to_be_hidden()
            expect(page.locator("[data-passkey-unavailable]")).to_be_visible()
            journeys.capture(page, root, f"{renewal_engine}-native-code-fallback")
            return
        cdp, authenticator_id = virtual_authenticator(page)
        _register(page, base)
        _opt_in(page)
        for width in journeys.WIDTHS:
            page.set_viewport_size({"width": width, "height": 850})
            journeys.capture(page, root, f"happy-own-credentials-{width}")
        journeys.sign_out(page, base)
        goto_settled(page, base + "/auth/login/")
        page.locator("#id_username").fill(manager["username"])
        page.locator("#id_password").fill(onboarding_staff["password"])
        assert (
            journeys.submit(page, 'form[action="/auth/login/"] button[type=submit]')
            == 200
        )
        goto_settled(page, base + "/auth/step-up/?next=/account/passkeys/")
        expect(page.locator("#passkey-step-up [data-passkey-start]")).to_be_visible()
        with page.expect_response(
            lambda response: response.url.endswith("/auth/passkey/assert/finish/")
        ) as stepped_up:
            page.locator("#passkey-step-up [data-passkey-start]").click()
        assert stepped_up.value.status == 200
        expect(page.locator("[data-passkey-record]")).to_have_count(1)
        journeys.capture(page, root, "happy-platform-step-up")
        journeys.sign_out(page, base)
        goto_settled(page, base + "/auth/login/")
        with page.expect_response(
            lambda response: response.url.endswith("/auth/passkey/assert/finish/")
        ) as verified:
            page.locator("#passkey-login [data-passkey-start]").click()
        assert verified.value.status == 200
        expect(page).not_to_have_url(base + "/auth/login/")
        goto_settled(page, base + "/account/sessions/")
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
    base = renewal_base_url.replace("127.0.0.1", "localhost", 1)
    root = renewal_artifact_root / f"passkey-{case}"
    engine = browser.browser_type.name
    manager = _organization_admin(onboarding_staff)
    with _recording(browser, root, expected_denial=True) as page:
        sign_in_manager(page, base, onboarding_staff, manager)
        if engine != "chromium":
            goto_settled(page, base + "/account/passkeys/")
            expect(page.locator('a[href^="/auth/step-up/"]')).to_be_visible()
            journeys.capture(page, root, f"{engine}-native-factor")
            return
        cdp, authenticator_id = virtual_authenticator(page)
        algorithm = _register(page, base)
        _opt_in(page)
        journeys.sign_out(page, base)
        goto_settled(page, base + "/auth/login/")
        with page.expect_response(
            lambda response: response.url.endswith("/auth/passkey/assert/finish/")
        ) as accepted:
            page.locator("#passkey-login [data-passkey-start]").click()
        assert accepted.value.status == 200
        expect(page).not_to_have_url(base + "/auth/login/")
        journeys.sign_out(page, base)
        goto_settled(page, base + "/auth/login/")
        if case == "foreign_rp":
            software, handle, sign_count = _software_credential(
                cdp, authenticator_id, algorithm=algorithm
            )
            csrf = page.locator(
                '#passkey-login input[name="csrfmiddlewaretoken"]'
            ).input_value()
            begin = page.request.post(
                base + "/auth/passkey/assert/begin/",
                form={"csrfmiddlewaretoken": csrf, "purpose": "login"},
            )
            assert begin.status == 200
            denied = page.request.post(
                base + "/auth/passkey/assert/finish/",
                form={
                    "csrfmiddlewaretoken": csrf,
                    "credential": software.assertion(
                        challenge=begin.json()["challenge"],
                        user_handle=handle,
                        options=AssertionOptions(
                            rp_id="foreign.example",
                            origin=base,
                            sign_count=sign_count,
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
                    "ON credential.id::text=event.affected_record_id "
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
    onboarding_staff: dict[str, str],
) -> None:
    browser = renewal_page.context.browser
    assert browser is not None
    base = renewal_base_url.replace("127.0.0.1", "localhost", 1)
    root = renewal_artifact_root / "passkey-errors"
    with _recording(browser, root, expected_abort=True) as page:
        context = page.context
        goto_settled(page, base + "/auth/login/")
        if renewal_engine != "chromium":
            expect(
                page.locator('form[action="/auth/login/"] button[type=submit]')
            ).to_be_visible()
            journeys.capture(page, root, f"{renewal_engine}-password-fallback")
            return
        manager = _organization_admin(onboarding_staff)
        sign_in_manager(page, base, onboarding_staff, manager)
        cdp, authenticator_id = virtual_authenticator(page)
        _register(page, base)
        _opt_in(page)
        journeys.sign_out(page, base)
        goto_settled(page, base + "/auth/login/")
        cdp.send(
            "WebAuthn.setAutomaticPresenceSimulation",
            {
                "authenticatorId": authenticator_id,
                "enabled": False,
            },
        )
        with page.expect_console_message(
            lambda message: message.text == "PASSKEY_PENDING:get"
        ):
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


def test_unexpected_client_exception_is_reported(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
) -> None:
    """A caught credential rejection must reach the test with its exact exception."""
    browser = renewal_page.context.browser
    assert browser is not None
    base = renewal_base_url.replace("127.0.0.1", "localhost", 1)
    root = renewal_artifact_root / "passkey-client-exception"
    with ExitStack() as recording:
        page = recording.enter_context(_recording(browser, root))
        page.add_init_script(
            """(() => {
                if (!navigator.credentials) {
                    Object.defineProperty(navigator, "credentials", {
                        configurable: true,
                        value: {
                            create: async () => {
                                throw new Error("Unexpected registration call");
                            }
                        }
                    });
                }
                if (!window.PublicKeyCredential) {
                    Object.defineProperty(window, "PublicKeyCredential", {
                        configurable: true,
                        value: function SyntheticPublicKeyCredential() {
                            throw new Error("Unexpected credential construction");
                        }
                    });
                }
                navigator.credentials.get = async () => {
                    throw new DOMException(
                        "Synthetic credential failure", "SecurityError"
                    );
                };
            })();"""
        )
        goto_settled(page, base + "/auth/login/")
        assert page.evaluate("window.isSecureContext") is True
        with page.expect_console_message(
            lambda message: message.text.startswith("PASSKEY_FAILURE:")
        ):
            page.locator("#passkey-login [data-passkey-start]").click()
        with pytest.raises(
            AssertionError, match="SecurityError: Synthetic credential failure"
        ):
            recording.close()
