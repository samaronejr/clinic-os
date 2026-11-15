"""J1: first clinic day (todo 75 ``journey-first-day``).

account creation -> organization/unit setup -> staff invitations and
recovery enrollment -> services/resources/templates -> approved provider
connections -> authorized migration rehearsal -> verified opening agenda.

Account creation posts the real signup form (a valid request stays pending
activation; it neither signs in nor opens a trial) and refuses an invalid
CNPJ. Staff invitations, the first sign-in with authenticator enrollment,
recovery enrollment with one-time codes, passkeys and an expired invitation
run the owning todo's proofs (15, 16). Clinic configuration is the versioned
settings publication with isolation and rejection (17); the provider step is
the onboarding checklist read by the reference organization's administrator,
where every listed capability must say "Not live eligible" and stays
BLOCKED-ON-EG. The migration rehearsal dry-runs a partner spreadsheet through
todo 66 (counts and rejection reasons reconcile; nothing is imported until a
signed cutover), and the opening agenda is the resources x time grid with
filters, saved views and the month drill (23).
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from django.utils.translation import gettext
from playwright.sync_api import expect

import renewal.browser.test_agenda_grid as grid
import renewal.browser.test_clinic_settings as settings_suite
import renewal.browser.test_onboarding as onboarding
import renewal.browser.test_onboarding_security as onboarding_security
import renewal.browser.test_passkeys as passkeys
from renewal.browser._fixture_secrets import FixtureSecret, new_password
from renewal.browser._navigation import click_to_navigate, goto_settled
from renewal.browser._onboarding import onboarding_staff
from renewal.browser._page_wait import await_autofocus
from renewal.browser.flagship_support import (
    capture,
    run_fixture,
    sign_in,
    staff_context,
)
from renewal.browser.test_acceptance_clinical import rerun
from renewal.browser.test_agenda import agenda_staff
from renewal.browser.test_agenda_grid import (
    board,
    grid_report,
    grid_root,
    grid_state,
    grid_world,
)
from renewal.browser.test_staff_intake import _search

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import Page

    from renewal.browser.flagship_support import FlagshipReport, ReferenceClinic

__all__ = (
    "agenda_staff",
    "board",
    "grid_report",
    "grid_root",
    "grid_state",
    "grid_world",
    "onboarding_staff",
)
# A checksum-valid synthetic CNPJ (the canonical documentation example).
VALID_CNPJ = "11.222.333/0001-81"
NOT_LIVE = "Not live eligible"


def test_j1_account_creation_stays_pending_activation(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    flagship: FlagshipReport,
) -> None:
    browser = renewal_page.context.browser
    assert browser is not None
    for width in (1280, 375):
        context = staff_context(browser, width, service_workers="block")
        try:
            page = context.new_page()
            goto_settled(page, renewal_base_url + "/auth/signup/")
            password = new_password()
            fields = {
                "organization_name": "Organização Sintética Primeiro Dia",
                "legal_responsible": "Responsável Sintético",
                "username": FixtureSecret(f"signup-{uuid4().hex}"),
                "email": FixtureSecret(f"signup-{uuid4().hex}@example.invalid"),
                "password": password,
                "password_confirm": password,
                "cnpj": VALID_CNPJ,
            }
            for name, value in fields.items():
                page.locator(f"#id_{name}").fill(value)
            click_to_navigate(page.locator("form button[type=submit]"))
            pending = page.locator('[data-signup-state="pending_activation"]')
            await_autofocus(pending)
            expect(pending).to_contain_text(
                gettext(
                    "Your organization is pending activation. This request has not "
                    "started a trial or opened the clinical workspace."
                )
            )
            capture(page, renewal_artifact_root, flagship.suite, f"signup-{width}")
        finally:
            context.close()
    flagship.cover("J1.account-creation", "happy", state="pending_activation")


def test_j1_invalid_cnpj_is_refused(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    flagship: FlagshipReport,
) -> None:
    rerun(
        flagship,
        "J1.account-creation",
        "failure",
        onboarding_security.test_invalid_cnpj_error_and_return_target,
        renewal_page=renewal_page,
        renewal_base_url=renewal_base_url,
        renewal_artifact_root=renewal_artifact_root,
        width=375,
    )


def test_j1_invitation_acceptance_enrollment_and_booking(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    onboarding_staff: dict[str, str],
    flagship: FlagshipReport,
) -> None:
    rerun(
        flagship,
        "J1.staff-invitations",
        "happy",
        onboarding.test_invite_accept_enroll_and_book,
        renewal_page=renewal_page,
        renewal_base_url=renewal_base_url,
        renewal_artifact_root=renewal_artifact_root,
        onboarding_staff=onboarding_staff,
        width=1280,
    )


def test_j1_expired_invitation_offers_a_reinvite(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    onboarding_staff: dict[str, str],
    flagship: FlagshipReport,
) -> None:
    rerun(
        flagship,
        "J1.staff-invitations",
        "failure",
        onboarding.test_expired_invitation_has_portuguese_reinvite_path,
        renewal_page=renewal_page,
        renewal_base_url=renewal_base_url,
        renewal_artifact_root=renewal_artifact_root,
        onboarding_staff=onboarding_staff,
        width=375,
    )


def test_j1_recovery_enrollment_codes_work_once(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    onboarding_staff: dict[str, str],
    flagship: FlagshipReport,
) -> None:
    rerun(
        flagship,
        "J1.recovery-enrollment",
        "adversarial",
        onboarding_security.test_recovery_reflow_once_only_and_next_request_revocation,
        renewal_page=renewal_page,
        renewal_base_url=renewal_base_url,
        renewal_artifact_root=renewal_artifact_root,
        onboarding_staff=onboarding_staff,
        width=1280,
    )


def test_j1_passkey_registration_and_sign_in(  # noqa: PLR0913 - owner proof fixtures
    renewal_page: Page,
    renewal_base_url: str,
    renewal_engine: str,
    renewal_artifact_root: Path,
    onboarding_staff: dict[str, str],
    flagship: FlagshipReport,
) -> None:
    rerun(
        flagship,
        "J1.recovery-enrollment",
        "happy",
        passkeys.test_platform_registration_step_up_and_sign_in,
        renewal_page=renewal_page,
        renewal_base_url=renewal_base_url,
        renewal_engine=renewal_engine,
        renewal_artifact_root=renewal_artifact_root,
        onboarding_staff=onboarding_staff,
    )


def test_j1_clinic_configuration_publishes_versions(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    flagship: FlagshipReport,
) -> None:
    rerun(
        flagship,
        "J1.services-templates",
        "failure",
        settings_suite.test_settings_native_publication_isolation_and_rejection,
        renewal_page=renewal_page,
        renewal_base_url=renewal_base_url,
        renewal_artifact_root=renewal_artifact_root,
        availability_staff=availability_staff,
        width=1280,
    )


def test_j1_provider_connections_are_review_only(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    reference_clinic: ReferenceClinic,
    flagship: FlagshipReport,
) -> None:
    reference = reference_clinic
    browser = renewal_page.context.browser
    assert browser is not None
    context = staff_context(browser, 1280)
    try:
        page = context.new_page()
        sign_in(page, renewal_base_url, reference, "org_admin")
        goto_settled(
            page,
            f"{renewal_base_url}/clinics/{reference.unit_a}/settings/onboarding/",
        )
        expect(
            page.get_by_text(
                gettext(
                    "Review only. This screen does not activate a provider or "
                    "authorize live use."
                )
            )
        ).to_be_visible()
        step = page.locator("li").filter(
            has=page.locator('form[action$="/providers/review/"]')
        )
        rows = step.locator("ul li").all_inner_texts()
        assert rows, "the checklist lists the provider connections"
        assert all(gettext(NOT_LIVE) in row for row in rows), rows
        keys = sorted({row.split(":", 1)[0].strip() for row in rows})
        capture(page, renewal_artifact_root, flagship.suite, "providers")
    finally:
        context.close()
    for key in keys:
        flagship.blocked(reference, key, "unit_a")
    flagship.cover("J1.provider-connections", "blocked", capabilities=len(keys))


def _session_key(page: Page) -> str:
    cookies = [c for c in page.context.cookies() if "session" in c["name"].lower()]
    assert len(cookies) == 1
    return str(cookies[0]["value"])


def test_j1_migration_rehearsal_imports_nothing_before_cutover(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    reference_clinic: ReferenceClinic,
    flagship: FlagshipReport,
) -> None:
    reference = reference_clinic
    browser = renewal_page.context.browser
    assert browser is not None
    operator = staff_context(browser, 1280)
    desk = staff_context(browser, 375)
    try:
        page = operator.new_page()
        sign_in(page, renewal_base_url, reference, "manager_a")
        rehearsal = run_fixture(
            reference.dsn,
            "import-rehearsal",
            {
                "actor": reference.personas["manager_a"].user_id,
                "clinic": reference.unit_a,
                "organization": reference.organization,
                "session": _session_key(page),
                "offset": "81100",
                "count": "5",
                "commit": "0",
            },
        )
        assert rehearsal["state"] == "dry_run"
        assert (rehearsal["rows"], rehearsal["valid"], rehearsal["rejected"]) == (
            8,
            5,
            3,
        )
        # Nothing was imported: the receptionist finds no rehearsal patient.
        reception = desk.new_page()
        sign_in(reception, renewal_base_url, reference, "reception_a1")
        goto_settled(
            reception,
            f"{renewal_base_url}/intake/clinics/{reference.unit_a}/patients/",
        )
        _search(reception, "Paciente Sintetico 081101")
        expect(reception.locator(".intake-empty")).to_be_visible()
        expect(
            reception.locator("tbody tr", has_text="Paciente Sintetico 081101")
        ).to_have_count(0)
        capture(reception, renewal_artifact_root, flagship.suite, "rehearsal-empty")
    finally:
        operator.close()
        desk.close()
    flagship.blocked(reference, "competitor_import_formats", "unit_a")
    flagship.adapter("import", "real", "apps.interop dry run (todo 66)")
    flagship.cover("J1.migration-rehearsal", "failure", rows=8, imported=0)


def test_j1_opening_agenda_views(board: object, flagship: FlagshipReport) -> None:
    rerun(
        flagship,
        "J1.opening-agenda",
        "happy",
        grid.test_filters_saved_views_and_the_month_drill,
        board=board,
    )


def test_j1_adapters_and_reference_clinic(
    reference_clinic: ReferenceClinic, flagship: FlagshipReport
) -> None:
    flagship.adapter("runtime", "real", "Gunicorn + Django as clinic_app, RLS")
    flagship.adapter("email", "synthetic", "invitation email through the outbox")
    for capability in ("email", "physician_registration", "managed_secrets"):
        flagship.blocked(reference_clinic, capability, "unit_a")
    assert len(reference_clinic.clinicians("unit_a")) == 6
    # Twelve clinicians; one of unit A's also consults at unit B.
    assert len(reference_clinic.clinicians("unit_b")) == 7
    assert (
        len(
            {
                persona.key
                for unit in ("unit_a", "unit_b")
                for persona in reference_clinic.clinicians(unit)
            }
        )
        == 12
    )
    assert sum(len(rooms) for rooms in reference_clinic.rooms.values()) == 4
