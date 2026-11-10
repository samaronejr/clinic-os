"""Real-browser care coordination: clinician request, staff bundle, refusals.

The physician asks for suggestions over a finalized plan; the synthetic model run
executes as ``clinic_app``; a clinic manager reviews and approves the previewed
administrative bundle with the login TOTP verification as the step-up; the
clinician accepts the clinical item. ``bundle.webm`` records the staff approval.
The failure journey shows an item-level validator refusal next to the approved
items; the adversarial journey has the fake model propose a medication change,
which the validator rejects without its text reaching the page or any sink.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
import pytest
from django.contrib.auth.hashers import make_password
from django.utils.translation import gettext
from django_otp.oath import TOTP
from playwright.sync_api import expect

from renewal.browser._fixture_secrets import new_totp_key
from renewal.browser._navigation import (
    click_to_navigate,
    expect_document,
    goto_settled,
    wait_for_signed_in,
)
from renewal.browser._totp_devices import reset_totp_device, seed_totp_device
from renewal.browser.a11y_support import check_page
from renewal.browser.engines import full_page_screenshot
from renewal.browser.test_availability import (
    _sign_in,
    _sign_in_physician,
    availability_staff,
)
from renewal.browser.test_patient_access import _tab_until

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import Browser, BrowserContext, Locator, Page

__all__ = ("availability_staff",)
OK = 200
_run_fixture = subprocess.run
# The reviewed synthetic plan sections replayed by care-coordination-v1.json.
BUNDLE_PLAN = (
    "Retorno em 6 semanas. Recepção confirma o agendamento do retorno. "
    "Reaplicar o PHQ-9 em 2 semanas."
)
INVALID_PLAN = (
    "Retorno em 6 semanas. Recepção confirma o agendamento do retorno. "
    "Revisão anual em 3 anos."
)
MEDICATION_PLAN = "Retorno em 6 semanas. Manter o acompanhamento combinado na consulta."
SENTINEL = "SINTETICO-SENTINELA-MEDICACAO"


def fixture(staff: dict[str, str], action: str, **extra: str) -> dict[str, object]:
    environment = {
        **os.environ,
        "APP_DATABASE_URL": staff["dsn"],
        "DJANGO_SETTINGS_MODULE": "config.settings.base",
        "CLINIC_DATA_MODE": "synthetic",
        "PYTHONPATH": ".:tests",
    }
    result = _run_fixture(
        [
            sys.executable,
            "-c",
            "import django\n"
            "django.setup()\n"
            "from renewal.care_coordination_fixture import main\n"
            "main()\n",
        ],
        input=json.dumps(
            {key: staff[key] for key in ("organization", "clinic_a", "physician_a_id")}
            | {"manager": staff["manager_id"], "action": action, **extra}
        ),
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    receipt: dict[str, object] = json.loads(result.stdout)
    return receipt


@pytest.fixture(scope="session")
def care_staff(availability_staff: dict[str, str]) -> dict[str, str]:
    """Add a TOTP clinic manager; both actors hold the workflow task bundle."""
    staff = availability_staff
    staff["manager"] = f"gerente-sintetica-{uuid4().hex[:4]}"
    staff["manager_id"] = str(uuid4())
    staff["manager_totp_key"] = new_totp_key()
    with psycopg.connect(staff["dsn"]) as connection:
        connection.execute(
            "SELECT set_config('app.current_tenant', %s, true)",
            [staff["organization"]],
        )
        connection.execute(
            "INSERT INTO clinic_app.identity_user "
            "(id, username, password, email, first_name, last_name, is_active, "
            "is_staff, is_superuser, date_joined) "
            "VALUES (%s, %s, %s, %s, '', '', true, false, false, now())",
            [
                staff["manager_id"],
                staff["manager"],
                make_password(staff["password"]),
                f"{staff['manager']}@care.invalid",
            ],
        )
        connection.execute(
            "INSERT INTO clinic_app.identity_userclinicrole "
            "(id, user_id, organization_id, clinic_id, role, "
            "workflow_bundle_version) VALUES (%s, %s, %s, %s, 'clinic_manager', 2)",
            [
                str(uuid4()),
                staff["manager_id"],
                staff["organization"],
                staff["clinic_a"],
            ],
        )
        connection.execute(
            "UPDATE clinic_app.identity_userclinicrole SET workflow_bundle_version = 2 "
            "WHERE user_id = %s AND clinic_id = %s",
            [staff["physician_a_id"], staff["clinic_a"]],
        )
        seed_totp_device(connection, staff["manager_id"], staff["manager_totp_key"])
    assert fixture(staff, "configure") == {"synthetic": True}
    return staff


def _context(browser: Browser, *, video: Path | None = None) -> BrowserContext:
    return browser.new_context(
        locale="pt-BR",
        timezone_id="Asia/Tokyo",
        viewport={"width": 1280, "height": 900},
        record_video_dir=str(video) if video else None,
    )


def _manager(page: Page, base: str, staff: dict[str, str]) -> None:
    """Sign the manager in; the login TOTP verification is the recent step-up."""
    staff["manager_totp_key"] = new_totp_key()
    with psycopg.connect(staff["dsn"]) as connection:
        reset_totp_device(connection, staff["manager_id"], staff["manager_totp_key"])
    _sign_in(page, base, staff["manager"], staff["password"])
    page.wait_for_url("**/auth/verify/**")
    token = TOTP(bytes.fromhex(staff["manager_totp_key"]), 30, 0, 6, 0).token()
    page.locator("#id_otp_token").fill(f"{token:06d}")
    click_to_navigate(page.locator("button[type=submit]"))
    wait_for_signed_in(page)


def _url(staff: dict[str, str], base: str) -> str:
    return f"{base}/clinics/{staff['clinic_a']}/automations/care-coordination/"


def _capture(page: Page, root: Path, name: str) -> None:
    page.emulate_media(reduced_motion="reduce")
    full_page_screenshot(page, root / f"{name}.png")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def _submit_with_keyboard(page: Page, button: Locator) -> None:
    """Reach the real control by Tab with a visible ring, then submit by Enter."""
    _tab_until(page, button, [], limit=120)
    with expect_document(page) as navigation:
        page.keyboard.press("Enter")
    assert navigation.value.status == OK


def _requested(page: Page, staff: dict[str, str], base: str, text: str) -> str:
    """The physician asks for suggestions; the worker runs the synthetic model."""
    plan = fixture(staff, "plan", text=text)["plan_id"]
    response = goto_settled(page, _url(staff, base))
    assert response is not None
    assert response.status == OK
    expect(page.get_by_role("heading", level=1)).to_have_text(
        gettext("Care coordination")
    )
    card = page.locator(f'[data-care-plan="{plan}"]')
    _submit_with_keyboard(
        page, card.get_by_role("button", name=gettext("Request suggestions"))
    )
    expect(page.locator("[data-care-generating]")).to_be_visible()
    invocation = page.locator(
        'form:has(input[name="action"][value="refresh"]) input[name="invocation_id"]'
    ).input_value()
    assert fixture(staff, "dispatch", invocation=invocation) == {
        "runtime_role": "clinic_app",
        "state": "succeeded",
    }
    _submit_with_keyboard(
        page, page.get_by_role("button", name=gettext("Refresh suggestions"))
    )
    expect(page.locator('[data-care-bundle="ready"]')).to_be_visible()
    return invocation


def _approve(page: Page, base: str, staff: dict[str, str], invocation: str) -> None:
    goto_settled(page, _url(staff, base))
    expect(page.locator("[data-care-bundles]")).to_be_visible()
    # Staff never see the clinician's plan list.
    expect(page.locator("[data-care-plans]")).to_have_count(0)
    row = page.locator(
        "form[data-care-bundle-row]"
        f':has(input[name="invocation_id"][value="{invocation}"])'
    )
    _submit_with_keyboard(
        page, row.get_by_role("button", name=gettext("Review bundle"))
    )
    expect(page.locator("[data-care-preview-item]")).to_have_count(2)
    _submit_with_keyboard(
        page, page.get_by_role("button", name=gettext("Approve this bundle"))
    )
    expect(page.locator('[data-care-outcome="approved"]')).to_have_count(2)


def test_clinician_request_and_staff_bundle_approval(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    care_staff: dict[str, str],
) -> None:
    staff, base, page = care_staff, renewal_base_url, renewal_page
    root = renewal_artifact_root / "care-coordination"
    root.mkdir(mode=0o700, exist_ok=True)
    _sign_in_physician(page, base, staff)
    invocation = _requested(page, staff, base, BUNDLE_PLAN)
    expect(page.locator("[data-care-item]")).to_have_count(3)
    expect(page.locator('[data-care-kind="questionnaire"]')).to_contain_text(
        gettext("Clinical: the clinician decides")
    )
    _capture(page, root, "clinician-suggestions")
    check_page(page, base, root, "care-clinician")
    browser = page.context.browser
    assert browser is not None
    context = _context(browser, video=root / "video")
    approver = context.new_page()
    try:
        _manager(approver, base, staff)
        _approve(approver, base, staff, invocation)
        expect(approver.locator("[data-care-outcomes]")).to_contain_text(
            gettext("Approved by staff")
        )
        _capture(approver, root, "bundle-approved")
        check_page(approver, base, root, "care-bundle")
    finally:
        context.close()
        if approver.video is not None:
            movie = root / "bundle.webm"
            approver.video.save_as(movie)
            movie.chmod(0o600)
    # The clinician decides the clinical item; the plan gains the action.
    goto_settled(page, _url(staff, base))
    opened = page.locator(
        'form:has(input[name="action"][value="open"])'
        f':has(input[name="invocation_id"][value="{invocation}"])'
    )
    _submit_with_keyboard(page, opened.get_by_role("button"))
    questionnaire = page.locator('[data-care-kind="questionnaire"]')
    _submit_with_keyboard(
        page, questionnaire.get_by_role("button", name=gettext("Accept into the plan"))
    )
    expect(page.locator("[data-care-saved]")).to_be_visible()
    expect(page.locator('[data-care-kind="questionnaire"]')).to_have_attribute(
        "data-care-state", "accepted"
    )
    receipt = fixture(staff, "inspect", invocation=invocation)
    assert receipt == {
        "actions": ["questionnaire", "return_window", "staff_coordination"],
        "items": [
            ["return_window", "approved", ""],
            ["staff_coordination", "approved", ""],
            ["questionnaire", "accepted", ""],
        ],
    }
    (root / "bundle-receipt.json").write_text(
        json.dumps(receipt, sort_keys=True) + "\n"
    )


def test_one_invalid_item_shows_an_item_level_refusal(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    care_staff: dict[str, str],
) -> None:
    staff, base, page = care_staff, renewal_base_url, renewal_page
    root = renewal_artifact_root / "care-coordination"
    root.mkdir(mode=0o700, exist_ok=True)
    _sign_in_physician(page, base, staff)
    invocation = _requested(page, staff, base, INVALID_PLAN)
    refused = page.locator('[data-care-item][data-care-state="refused"]')
    expect(refused).to_have_count(1)
    expect(refused.locator("[data-care-reason]")).to_have_text(
        gettext("The proposed window is outside the allowed range.")
    )
    expect(page.locator('[data-care-item][data-care-state="pending"]')).to_have_count(2)
    _capture(page, root, "item-level-refusal")
    check_page(page, base, root, "care-refusal")
    browser = page.context.browser
    assert browser is not None
    context = _context(browser)
    approver = context.new_page()
    try:
        _manager(approver, base, staff)
        _approve(approver, base, staff, invocation)
    finally:
        context.close()
    assert fixture(staff, "inspect", invocation=invocation)["items"] == [
        ["return_window", "approved", ""],
        ["staff_coordination", "approved", ""],
        ["return_window", "refused", "window_invalid"],
    ]


def test_fake_medication_change_is_rejected_by_the_validator(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    care_staff: dict[str, str],
) -> None:
    staff, base, page = care_staff, renewal_base_url, renewal_page
    root = renewal_artifact_root / "care-coordination"
    root.mkdir(mode=0o700, exist_ok=True)
    _sign_in_physician(page, base, staff)
    invocation = _requested(page, staff, base, MEDICATION_PLAN)
    expect(page.locator('[data-care-item][data-care-state="refused"]')).to_have_count(2)
    forbidden = page.locator('[data-care-kind="unsupported"]')
    expect(forbidden).to_contain_text(gettext("Action type not allowed"))
    expect(forbidden.locator("[data-care-reason]")).to_have_text(
        gettext(
            "The assistant may not propose this kind of action. Only the clinician "
            "decides medication, procedures, referrals and patient guidance."
        )
    )
    assert SENTINEL not in page.content()
    _capture(page, root, "medication-rejected")
    assert fixture(staff, "inspect", invocation=invocation)["items"] == [
        ["return_window", "pending", ""],
        ["unsupported", "refused", "kind_forbidden"],
        ["referral", "refused", "kind_forbidden"],
    ]
    sinks = fixture(staff, "sinks", sentinel=SENTINEL)
    assert sinks == {
        "audit_clean": True,
        "last_error_clean": True,
        "outbox_clean": True,
        "proposals_clean": True,
    }
    (root / "sink-capture.json").write_text(json.dumps(sinks, sort_keys=True) + "\n")
    assert page.evaluate(
        "sentinel => !JSON.stringify({local: {...localStorage}, "
        "session: {...sessionStorage}}).includes(sentinel)",
        SENTINEL,
    )


@pytest.mark.parametrize(
    "scene",
    [
        ("phone", 375, 812, 1),
        ("reflow-200", 640, 450, 2),
        ("reflow-400", 320, 225, 4),
    ],
)
def test_keyboard_bundle_and_refusal_reflow(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    care_staff: dict[str, str],
    scene: tuple[str, int, int, int],
) -> None:
    """Run the feature at narrow and equivalent 200%/400% CSS viewports."""
    name, width, height, scale = scene
    staff, base = care_staff, renewal_base_url
    root = renewal_artifact_root / "care-coordination" / name
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    browser = renewal_page.context.browser
    assert browser is not None
    with (
        browser.new_context(
            locale="pt-BR",
            timezone_id="Asia/Tokyo",
            viewport={"width": width, "height": height},
            device_scale_factor=scale,
            reduced_motion="reduce",
        ) as clinician_context,
        browser.new_context(
            locale="pt-BR",
            timezone_id="Asia/Tokyo",
            viewport={"width": width, "height": height},
            device_scale_factor=scale,
            reduced_motion="reduce",
        ) as staff_context,
    ):
        page, approver = clinician_context.new_page(), staff_context.new_page()
        _sign_in_physician(page, base, staff)
        invocation = _requested(page, staff, base, INVALID_PLAN)
        expect(
            page.locator('[data-care-item][data-care-state="refused"]')
        ).to_have_count(1)
        _capture(page, root, "clinician-refusal")
        check_page(page, base, root, "clinician-refusal")
        _manager(approver, base, staff)
        goto_settled(approver, _url(staff, base))
        expect(approver.locator("[data-care-plans]")).to_have_count(0)
        row = approver.locator(
            "form[data-care-bundle-row]"
            f':has(input[name="invocation_id"][value="{invocation}"])'
        )
        _submit_with_keyboard(
            approver, row.get_by_role("button", name=gettext("Review bundle"))
        )
        expect(approver.locator("[data-care-preview-item]")).to_have_count(2)
        focused: list[str] = []
        _tab_until(
            approver,
            approver.get_by_role("button", name=gettext("Approve this bundle")),
            focused,
            limit=120,
        )
        _capture(approver, root, "staff-preview-keyboard")
        check_page(approver, base, root, "staff-preview")
        with expect_document(approver) as navigation:
            approver.keyboard.press("Enter")
        assert navigation.value.status == OK
        expect(approver.locator('[data-care-outcome="approved"]')).to_have_count(2)
        _capture(approver, root, "staff-approved")
        check_page(approver, base, root, "staff-approved")
        observed = approver.evaluate(
            "({width: innerWidth, height: innerHeight, scale: devicePixelRatio})"
        )
        assert observed == {"width": width, "height": height, "scale": scale}
        (root / "keyboard-reflow.json").write_text(
            json.dumps(
                {
                    "scene": name,
                    "viewport": observed,
                    "approval_tab_stops": focused,
                    "request_review_approve": "Tab and Enter with visible focus rings",
                    "refused_items": 1,
                    "approved_items": 2,
                    "horizontal_overflow": False,
                    "zoom_method": "equivalent CSS viewport and device scale factor",
                },
                sort_keys=True,
            )
            + "\n"
        )
