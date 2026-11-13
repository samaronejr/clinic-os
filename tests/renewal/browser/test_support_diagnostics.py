"""Todo 78: masked support diagnostics, approved and revoked through the real UI.

A customer organization admin approves time-limited support access on the
governance screen. A named support user from a separate organization signs in
with their own authenticator in a second browser context, opens the masked
diagnostics with the selectors in the POST body, and sees counts and closed
labels only: a sentinel planted in outbox error text and the customer's staff
usernames never reach the page. The same support session is refused the
patient list, and after the admin revokes the approval the screen refuses it
too. Captures go to ``<artifact_root>/support-diagnostics/``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Final
from uuid import uuid4

import psycopg
import pytest
from django.contrib.auth.hashers import make_password
from django.utils.translation import gettext
from playwright.sync_api import expect

from renewal.browser import a11y_support
from renewal.browser._fixture_secrets import new_totp_key
from renewal.browser._navigation import click_to_navigate, goto_settled
from renewal.browser._totp_devices import seed_totp_device
from renewal.browser.engines import full_page_screenshot
from renewal.browser.support_managers import seed_ai_admin
from renewal.browser.test_availability import availability_staff
from renewal.browser.test_patient_access import _no_overflow
from renewal.browser.test_retention import sign_in_manager

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import Page

__all__ = ("availability_staff",)

WIDTHS: Final = (1280, 375)
FORBIDDEN: Final = 403
OK: Final = 200
SENTINEL: Final = "SENTINEL-SUPPORT-OUTBOX-41c9"


def capture(page: Page, root: Path, state: str, width: int) -> str:
    """Store one privacy-safe capture and prove the width holds no overflow."""
    folder = root / "support-diagnostics"
    folder.mkdir(exist_ok=True, mode=0o700)
    _no_overflow(page)
    path = folder / f"{state}-{width}.png"
    full_page_screenshot(page, path)
    path.chmod(0o600)
    return path.name


def _org_admin(staff: dict[str, str]) -> dict[str, str]:
    manager = seed_ai_admin(staff, "light")
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant',%s,true)", [staff["organization"]]
        )
        clinics = conn.execute(
            "SELECT c.id FROM clinic_app.identity_clinic c "
            "WHERE c.organization_id=%s AND NOT EXISTS "
            "(SELECT 1 FROM clinic_app.identity_userclinicrole r "
            "WHERE r.clinic_id=c.id AND r.user_id=%s AND r.role='org_admin')",
            [staff["organization"], manager["id"]],
        ).fetchall()
        for (clinic,) in clinics:
            conn.execute(
                "INSERT INTO clinic_app.identity_userclinicrole "
                "(id,user_id,organization_id,clinic_id,role,workflow_bundle_version) "
                "VALUES (%s,%s,%s,%s,'org_admin',1)",
                [str(uuid4()), manager["id"], staff["organization"], str(clinic)],
            )
    return manager


def _support_user(staff: dict[str, str]) -> dict[str, str]:
    """A named support account that belongs to its own vendor organization."""
    organization, clinic, user = str(uuid4()), str(uuid4()), str(uuid4())
    support = {
        "id": user,
        "username": f"suporte-sintetico-{uuid4().hex[:6]}",
        "totp_key": new_totp_key(),
    }
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute("SELECT set_config('app.current_tenant',%s,true)", [organization])
        conn.execute(
            "INSERT INTO clinic_app.identity_organization "
            "(id,name,cnpj,status,passkey_step_up_enabled) "
            "VALUES (%s,'Synthetic Support Vendor',%s,'active',false)",
            [organization, f"{int(uuid4()) % 10**14:014d}"],
        )
        conn.execute(
            "INSERT INTO clinic_app.identity_clinic "
            "(id,organization_id,name,crm_uf,timezone) "
            "VALUES (%s,%s,'Synthetic Support Desk','SP','America/Sao_Paulo')",
            [clinic, organization],
        )
        conn.execute(
            "INSERT INTO clinic_app.identity_user "
            "(id,username,password,email,first_name,last_name,is_active,"
            "is_staff,is_superuser,date_joined) "
            "VALUES (%s,%s,%s,%s,'','',true,false,false,now())",
            [
                user,
                support["username"],
                make_password(staff["password"]),
                f"{support['username']}@support.invalid",
            ],
        )
        conn.execute(
            "INSERT INTO clinic_app.identity_userclinicrole "
            "(id,user_id,organization_id,clinic_id,role,workflow_bundle_version) "
            "VALUES (%s,%s,%s,%s,'clinic_admin',1)",
            [str(uuid4()), user, organization, clinic],
        )
        seed_totp_device(conn, user, support["totp_key"])
    return support


def _plant_outbox_sentinel(staff: dict[str, str]) -> None:
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant',%s,true)", [staff["organization"]]
        )
        conn.execute(
            "INSERT INTO clinic_app.comms_integrationoperation "
            "(id,organization_id,clinic_id,actor_id,kind,channel,payload_digest,"
            "provider,subject_type,subject_id,status,attempt_count,max_attempts,"
            "last_error,idempotency_key,created_at,updated_at) "
            "VALUES (%s,%s,%s,%s,'communication','email','','synthetic',"
            "'synthetic.support',%s,'failed',1,5,%s,%s,now(),now())",
            [
                str(uuid4()),
                staff["organization"],
                staff["clinic_a"],
                staff["receptionist_id"],
                str(uuid4()),
                SENTINEL,
                str(uuid4()),
            ],
        )


def _approve(page: Page, base: str, staff: dict[str, str], support_id: str) -> str:
    response = goto_settled(
        page, f"{base}/clinics/{staff['clinic_a']}/settings/governance/"
    )
    assert response is not None
    assert response.status == OK
    form = page.locator("form:has(input[name=action][value=support])")
    form.locator("[name=support-support_user_id]").fill(support_id)
    form.locator("[name=support-purpose]").fill("Synthetic reminder delay diagnosis")
    form.locator("[name=support-minutes]").fill("30")
    click_to_navigate(form.locator("button[type=submit]"))
    grants = page.locator(
        "section[aria-labelledby=support-grants-title] input[name=grant_id]"
    )
    expect(grants.first).to_be_attached()
    return grants.first.input_value()


def _open(page: Page, base: str, clinic: str, grant: str) -> int:
    goto_settled(page, f"{base}/support/diagnostics/")
    page.locator("#support-clinic").fill(clinic)
    page.locator("#support-grant").fill(grant)
    with page.expect_response(f"{base}/support/diagnostics/") as response:
        click_to_navigate(page.get_by_role("button", name=gettext("Open diagnostics")))
    return response.value.status


@pytest.mark.parametrize("width", WIDTHS)
def test_support_reads_masked_diagnostics_until_the_customer_revokes(  # noqa: PLR0913
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    browser_report: dict[str, object],
    width: int,
) -> None:
    staff = availability_staff
    base = renewal_base_url
    admin = renewal_page
    admin.set_viewport_size({"width": width, "height": 900})
    manager = _org_admin(staff)
    support = _support_user(staff)
    _plant_outbox_sentinel(staff)

    # The customer admin approves masked access for the named support account.
    sign_in_manager(admin, base, staff, manager)
    grant = _approve(admin, base, staff, support["id"])

    browser = admin.context.browser
    assert browser is not None
    context = browser.new_context(
        locale="pt-BR",
        timezone_id="Asia/Tokyo",
        viewport={"width": width, "height": 900},
    )
    try:
        page = context.new_page()
        sign_in_manager(page, base, staff, support)

        # Happy path: counts and closed labels only.
        assert _open(page, base, staff["clinic_a"], grant) == OK
        ready = page.locator("[data-support-diagnostics][data-state=ready]")
        expect(ready).to_be_visible()
        expect(page.locator("[data-support-queue]").first).to_be_visible()
        expect(page.locator("[data-support-provider]").first).to_be_visible()
        body = page.content()
        for hidden in (
            SENTINEL,
            staff["receptionist"],
            staff["physician_a"],
            manager["username"],
            grant,
            "Synthetic reminder delay diagnosis",
        ):
            assert hidden not in body
        axe = a11y_support.check_page(
            page, base, renewal_artifact_root, f"support-ready-{width}"
        )
        ready_capture = capture(page, renewal_artifact_root, "ready", width)

        # Adversarial: the support session cannot open the customer's patients.
        chart = goto_settled(
            page, f"{base}/intake/clinics/{staff['clinic_a']}/patients/"
        )
        assert chart is not None
        assert chart.status != OK
        assert SENTINEL not in page.content()

        # Failure: once the customer revokes, the same session is refused.
        click_to_navigate(
            admin.get_by_role("button", name=gettext("Revoke support access")).first
        )
        assert _open(page, base, staff["clinic_a"], grant) == FORBIDDEN
        expect(page.locator("[data-support-denied]")).to_be_visible()
        expect(page.locator("[data-support-ready]")).to_have_count(0)
        denied_capture = capture(page, renewal_artifact_root, "revoked", width)
    finally:
        context.close()
    checks = browser_report.setdefault("checks", [])
    assert isinstance(checks, list)
    checks.append(
        {
            "suite": "support-diagnostics",
            "width": width,
            "ready": ready_capture,
            "revoked": denied_capture,
            "patient_list_status": chart.status,
            "axe": json.loads(json.dumps(axe)),
        }
    )
