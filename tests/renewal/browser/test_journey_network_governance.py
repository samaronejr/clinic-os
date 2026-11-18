"""J7: network governance (todo 75 ``journey-network-governance``).

A multi-unit organization delegates access, inspects activity, updates a
template safely, rotates a provider credential and demonstrates restored
access to encrypted records after a recovery exercise.

On the D-17 reference organization (two units) the organization
administrator works the real Enterprise governance screen: both units and
their members are listed, and revoking one clinician's unit access takes
effect on that clinician's open session with the same denial an unknown unit
gets. Activity inspection is the patient access log listing delegate reads
(todo 19/73). A template update is the versioned clinic configuration
publication with cross-clinic isolation and stale-version rejection (17). The
partner API credential is rotated through the management API: the old secret
is refused at the OAuth token endpoint and a stale rotation conflicts. The
recovery exercise removes the tenant key escrow from the running deployment
(protected records then refuse to render) and restores it (the same record
reads again). Real OIDC/SCIM providers, KMS and provider PITR stay
BLOCKED-ON-EG.
"""

# Injects SINTETICO-SENTINELA-* values into protected fields (runner scan).
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Final
from uuid import uuid4

from django.utils.translation import gettext
from ops.testing.sentinel_scan import new_sentinel
from playwright.sync_api import expect

import renewal.browser.test_clinic_settings as settings_suite
import renewal.browser.test_portal as portal
import renewal.browser.test_workspace as workspace
from renewal.browser._navigation import click_to_navigate, goto_settled
from renewal.browser._page_wait import wait_for_js
from renewal.browser.flagship_support import capture, sign_in, staff_context
from renewal.browser.test_acceptance_clinical import rerun
from renewal.browser.test_acceptance_operations import _org_operator
from renewal.browser.test_billing import seed_patient
from renewal.browser.test_retention import sign_in_manager
from renewal.browser.test_staff_intake import SEARCH, SETTLED_JS, _search
from renewal.browser.test_workspace import desktop, workspace_browser, workspace_staff

if TYPE_CHECKING:
    from playwright.sync_api import Page, Request, Response

    from renewal.browser.flagship_support import FlagshipReport, ReferenceClinic

__all__ = ("desktop", "workspace_browser", "workspace_staff")
OK: Final = 200
MANAGEMENT: Final = "/api/interop/v1/"
TOKEN_PATH: Final = "/api/v1/oauth/token"  # noqa: S105 - a URL path
KEK_FILE: Final = "tenant-kek.secret"


def _csrf(page: Page) -> str:
    tokens = [c for c in page.context.cookies() if "csrf" in c["name"].lower()]
    assert len(tokens) == 1
    return str(tokens[0]["value"])


def test_j7_adapters_and_provider_legs(
    reference_clinic: ReferenceClinic, flagship: FlagshipReport
) -> None:
    flagship.adapter("runtime", "real", "Gunicorn + Django as clinic_app, RLS")
    flagship.adapter("identity_provider", "synthetic", "synthetic OIDC/SCIM issuer")
    flagship.adapter("partner_api", "real", "OAuth2 client credentials, hashed secrets")
    for capability in ("managed_secrets", "tenant_key_management", "hosted_pitr"):
        flagship.blocked(reference_clinic, capability, "unit_a")


def _reload_evidence(response: Response, landed: str) -> str:
    """Say how a revoked session's reload was answered: worker, cache or server."""
    chain: list[str] = []
    request: Request | None = response.request
    while request is not None:
        chain.append(request.url)
        request = request.redirected_from
    return (
        f"revoked session reopened {landed}: response {response.url} "
        f"status {response.status}, "
        f"from_service_worker={response.from_service_worker}, "
        f"redirects {list(reversed(chain))}, "
        f"cache-control={response.headers.get('cache-control')!r}"
    )


def test_j7_unit_access_revocation_reaches_the_open_session(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    reference_clinic: ReferenceClinic,
    flagship: FlagshipReport,
) -> None:
    reference = reference_clinic
    browser = renewal_page.context.browser
    assert browser is not None
    clinician = reference.personas["physician_a2"]
    agenda = f"{renewal_base_url}/scheduling/clinics/{reference.unit_b}/agenda/"
    staff = staff_context(browser, 375)
    admin = staff_context(browser, 1280)
    try:
        own = staff.new_page()
        sign_in(own, renewal_base_url, reference, "physician_a2")
        opened = goto_settled(own, agenda)
        assert opened is not None
        assert opened.status == OK
        page = admin.new_page()
        sign_in(page, renewal_base_url, reference, "org_admin")
        goto_settled(
            page, f"{renewal_base_url}/clinics/{reference.unit_a}/settings/governance/"
        )
        expect(page.locator("[data-enterprise-governance]")).to_be_visible()
        for unit in (reference.unit_a, reference.unit_b):
            expect(page.get_by_text(unit).first).to_be_visible()
        expect(page.get_by_text(clinician.user_id).first).to_be_visible()
        capture(page, renewal_artifact_root, flagship.suite, "governance")
        form = page.locator('form:has(input[name="action"][value="unit"])')
        form.locator('[name="unit-unit_id"]').fill(reference.unit_b)
        form.locator('[name="unit-user_id"]').fill(clinician.user_id)
        click_to_navigate(form.locator("button[type=submit]"))
        expect(page.get_by_text(gettext("Governance change recorded."))).to_be_visible()
        # The membership change ends the clinician's open session...
        reopened = goto_settled(own, agenda)
        assert reopened is not None
        assert "/auth/login/" in reopened.url, _reload_evidence(reopened, own.url)
        assert "/auth/login/" in own.url, _reload_evidence(reopened, own.url)
        # ...and after signing in again unit A still opens while unit B is
        # refused exactly like a unit that does not exist.
        sign_in(own, renewal_base_url, reference, "physician_a2")
        home = goto_settled(
            own, f"{renewal_base_url}/scheduling/clinics/{reference.unit_a}/agenda/"
        )
        revoked = goto_settled(own, agenda)
        unknown = goto_settled(
            own, f"{renewal_base_url}/scheduling/clinics/{uuid4()}/agenda/"
        )
        assert home is not None
        assert home.status == OK
        assert revoked is not None
        assert unknown is not None
        assert revoked.status == unknown.status
        assert revoked.status in {403, 404}
        capture(own, renewal_artifact_root, flagship.suite, "revoked")
    finally:
        staff.close()
        admin.close()
    flagship.cover(
        "J7.delegate-access",
        "adversarial",
        session_ended=True,
        identical_denial=True,
        unit="unit_b",
    )


def test_j7_activity_lists_delegate_reads(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    flagship: FlagshipReport,
) -> None:
    rerun(
        flagship,
        "J7.inspect-activity",
        "happy",
        portal.test_access_log_lists_delegate_reads_without_emergency_review,
        renewal_page=renewal_page,
        renewal_base_url=renewal_base_url,
        renewal_artifact_root=renewal_artifact_root,
        availability_staff=availability_staff,
        width=375,
    )


def test_j7_template_update_is_versioned_and_isolated(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    flagship: FlagshipReport,
) -> None:
    rerun(
        flagship,
        "J7.template-update",
        "failure",
        settings_suite.test_settings_native_publication_isolation_and_rejection,
        renewal_page=renewal_page,
        renewal_base_url=renewal_base_url,
        renewal_artifact_root=renewal_artifact_root,
        availability_staff=availability_staff,
        width=375,
    )


def test_j7_revoked_membership_drops_the_stale_clinic_context(
    desktop: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    workspace_staff: dict[str, str],
    flagship: FlagshipReport,
) -> None:
    rerun(
        flagship,
        "J7.delegate-access",
        "failure",
        workspace.test_stale_clinic_context_is_dropped_after_revocation,
        desktop=desktop,
        renewal_base_url=renewal_base_url,
        renewal_artifact_root=renewal_artifact_root,
        workspace_staff=workspace_staff,
    )


def _manage(page: Page, base: str, body: dict[str, object]) -> tuple[int, object]:
    response = page.request.post(
        base + MANAGEMENT,
        data=json.dumps(body),
        headers={
            "Content-Type": "application/json",
            "X-CSRFToken": _csrf(page),
            "Referer": page.url,
        },
    )
    return response.status, response.json()


def _token(page: Page, base: str, client: str, secret: str) -> int:
    basic = base64.b64encode(f"{client}:{secret}".encode()).decode("ascii")
    response = page.context.request.post(
        base + TOKEN_PATH,
        form={"grant_type": "client_credentials"},
        headers={"Authorization": f"Basic {basic}"},
    )
    return response.status


def test_j7_partner_credential_rotation_refuses_the_old_secret(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    reference_clinic: ReferenceClinic,
    flagship: FlagshipReport,
) -> None:
    reference = reference_clinic
    operator = _org_operator(reference)
    browser = renewal_page.context.browser
    assert browser is not None
    context = staff_context(browser, 1280)
    try:
        page = context.new_page()
        sign_in_manager(
            page,
            renewal_base_url,
            {"dsn": reference.dsn, "password": reference.password},
            operator,
        )
        status, created = _manage(
            page,
            renewal_base_url,
            {
                "clinic_id": reference.unit_a,
                "action": "clients.create",
                "name": "Parceiro Sintético",
                "scopes": ["appointments:read"],
                "rate_limit_per_minute": 60,
                "command_id": str(uuid4()),
            },
        )
        assert status == 201, status
        assert isinstance(created, dict)
        client, first = str(created["client_id"]), str(created["client_secret"])
        before = _token(page, renewal_base_url, client, first)
        status, rotated = _manage(
            page,
            renewal_base_url,
            {
                "clinic_id": reference.unit_a,
                "action": "clients.rotate",
                "client_id": client,
                "expected_revision": created["revision"],
            },
        )
        assert status == OK, status
        assert isinstance(rotated, dict)
        second = str(rotated["client_secret"])
        assert second != first
        assert int(rotated["revision"]) > int(str(created["revision"]))
        old = _token(page, renewal_base_url, client, first)
        new = _token(page, renewal_base_url, client, second)
        assert old != OK
        assert new in {OK, 503}
        # A rotation from the superseded revision conflicts instead of applying.
        stale, _body = _manage(
            page,
            renewal_base_url,
            {
                "clinic_id": reference.unit_a,
                "action": "clients.rotate",
                "client_id": client,
                "expected_revision": created["revision"],
            },
        )
        assert stale == 409
        status, listed = _manage(
            page,
            renewal_base_url,
            {"clinic_id": reference.unit_a, "action": "clients.list"},
        )
        assert status == OK
        assert first not in json.dumps(listed)
        assert second not in json.dumps(listed)
    finally:
        context.close()
    flagship.cover(
        "J7.rotate-credential",
        "adversarial",
        token_before=before,
        old_secret_status=old,
        new_secret_status=new,
        stale_rotation=409,
    )


def test_j7_encrypted_records_read_again_after_key_recovery(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    reference_clinic: ReferenceClinic,
    flagship: FlagshipReport,
) -> None:
    reference = reference_clinic
    secret_dir = Path(os.environ["CLINIC_SECRET_DIR"])
    kek = secret_dir / KEK_FILE
    escrow = secret_dir / f"{KEK_FILE}.escrow"
    name = f"Paciente {new_sentinel('GOVERNANCE-NAME')}"
    seed_patient(
        {
            "dsn": reference.dsn,
            "organization": reference.organization,
            "clinic_a": reference.unit_b,
            "receptionist_id": reference.personas["reception_b1"].user_id,
        },
        name,
    )
    flagship.sentinel("GOVERNANCE-NAME")
    browser = renewal_page.context.browser
    assert browser is not None
    context = staff_context(browser, 1280)
    try:
        page = context.new_page()
        sign_in(page, renewal_base_url, reference, "reception_b1")
        patients = f"{renewal_base_url}/intake/clinics/{reference.unit_b}/patients/"
        goto_settled(page, patients)
        _search(page, name)
        expect(page.locator("tbody tr", has_text=name)).to_have_count(1)
        kek.rename(escrow)
        try:
            page.locator("#id_q").fill(name)
            with page.expect_response(
                lambda response: (
                    response.request.method == "POST" and "/patients/" in response.url
                )
            ) as refused:
                page.locator(SEARCH).click()
            wait_for_js(page, SETTLED_JS)
            assert refused.value.status >= 500 or refused.value.status == 403
            # The refusal renders no protected value from the rows alone.
            assert name not in refused.value.text()
        finally:
            escrow.rename(kek)
        goto_settled(page, patients)
        _search(page, name)
        expect(page.locator("tbody tr", has_text=name)).to_have_count(1)
        capture(page, renewal_artifact_root, flagship.suite, "key-restored")
    finally:
        context.close()
    flagship.cover(
        "J7.recovery-exercise",
        "failure",
        missing_key_status=refused.value.status,
        restored=True,
    )
