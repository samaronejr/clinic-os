"""Todo 50 patient portal v2 in a real browser, served as ``clinic_app``.

Owner access only seeds synthetic patients, invitations, an availability
window and a questionnaire draft; every patient action goes through a screen.
Every wait subscribes to a navigation, a response or a DOM state.

Evidence grid: the home runs at 1280, 768 and 375 px for a patient holding
every operation and for one holding only ``enrollment_view``; the summary test
books through the portal, receives a released synthetic charge, opens its
non-payable instructions and, once the clinic attests the synthetic
settlement, finds the rehearsal receipt (the patient's clip is recorded for
the QA artifact); the recovery test saves a questionnaire draft, loses
the session, re-enters with a new code and finds the draft; the matrix test
adds 320 px reflow, forced colors, reduced motion, 200% zoom, the keyboard
path and the JavaScript-off baseline; the destination test opens every
destination (and the waitlist offers page) at 1280, 375 and 320 px and finds
exactly one way back and one link to the visible non-AI support panel first;
the access gate shows the same panel; the data-rights test files a correction and an
access request and downloads the verifiable copy on an emulated phone; the
staff-queue test has management start and answer a filed correction, which the
patient then sees answered; the preferences test keeps display choices
server-side. No page registers a service
worker, asks for notification permission or writes browser storage. Axe must
report zero violations of any impact on every portal state captured here.
"""

from __future__ import annotations

import hashlib
import secrets
from typing import TYPE_CHECKING, Final, Literal
from uuid import uuid4

import psycopg
import pytest
from django.utils.translation import gettext
from playwright.sync_api import expect
from psycopg.types.json import Jsonb

from renewal.browser._fixture_secrets import new_access_code
from renewal.browser._navigation import click_to_navigate, goto_settled
from renewal.browser._page_wait import evaluate_js
from renewal.browser._protected import encrypt
from renewal.browser.a11y_support import check_page
from renewal.browser.engines import (
    full_page_screenshot,
    mobile_context,
    new_context,
    zoom_200,
)
from renewal.browser.test_availability import availability_staff
from renewal.browser.test_billing import (
    AMOUNT,
    CODE_PREFIX,
    confirm_settlement,
    create_charge,
    expect_state,
    issue_and_code,
    ledger_url,
)
from renewal.browser.test_encounter import press
from renewal.browser.test_patient_access import (
    MIN_TARGET_PX,
    _no_overflow,
    _overflowing,
    _redeem,
    _tab_until,
    _watch_errors,
)
from renewal.browser.test_questionnaires import QUESTIONS
from renewal.browser.test_retention import seed_manager, sign_in_manager
from renewal.browser.test_self_booking import _choose_day

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import Browser, BrowserContext, Page
    from psycopg.rows import TupleRow

__all__ = ("availability_staff",)

ALL_OPERATIONS: Final = (
    "enrollment_view",
    "questionnaires",
    "booking",
    "records",
    "consent",
    "teleconsult",
    "billing",
)
DESTINATION_ORDER: Final = (
    "appointments",
    "questionnaires",
    "teleconsult",
    "results",
    "documents",
    "records",
    "charges",
    "consent",
    "preferences",
    "data-requests",
)
# Destinations keyed to ``enrollment_view`` stay open for a summary-only session.
LIMITED_ORDER: Final = ("preferences", "data-requests")
CORRECTION: Final = "Sintético: o telefone do cadastro mudou."
MATRIX_WIDTHS: Final = (1280, 768, 375)
DESTINATION_WIDTHS: Final = (1280, 375, 320)
HOME_PATH: Final = "/patient/"
DRAFT: Final = "Rascunho sintético preservado"
FORBIDDEN: Final = 403
TARGETS_JS: Final = (
    "Array.from(document.querySelectorAll("
    "'main a[href], main button, main input:not([type=hidden]), main select,"
    " main summary'))"
    ".filter((element) => element.getClientRects().length > 0)"
    ".map((element) => { const box = element.getBoundingClientRect();"
    " return [element.id || element.textContent.trim().slice(0, 40),"
    " Math.round(box.width), Math.round(box.height)]; })"
)
STORAGE_JS: Final = """async () => ({
  local: window.localStorage.length,
  session: Object.entries(window.sessionStorage),
  caches: 'caches' in window ? (await caches.keys()).length : 0,
  databases: indexedDB.databases ? (await indexedDB.databases()).length : 0,
  workers: navigator.serviceWorker
    ? (await navigator.serviceWorker.getRegistrations()).length : 0,
  notifications: 'Notification' in window ? Notification.permission : 'default',
})"""
FONT_SIZE_JS: Final = "getComputedStyle(document.documentElement).fontSize"


def _seed(
    staff: dict[str, str],
    *,
    name: str,
    operations: tuple[str, ...],
    day: str | None = None,
    questionnaire: bool = False,
) -> dict[str, str]:
    data = {
        key: str(uuid4())
        for key in ("patient", "enrollment", "grant", "template", "response")
    }
    data["code"] = new_access_code()
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant', %s, true)", [staff["organization"]]
        )
        conn.execute(
            "INSERT INTO clinic_app.intake_patient "
            "(id,organization_id,full_name,birth_date,created_at) "
            "VALUES (%s,%s,%s,%s,now())",
            [
                data["patient"],
                staff["organization"],
                encrypt(conn, "intake.patient.full_name", name.encode()),
                encrypt(conn, "intake.patient.birth_date", b"1990-01-01"),
            ],
        )
        conn.execute(
            "INSERT INTO clinic_app.intake_patientclinicenrollment "
            "(id,organization_id,clinic_id,patient_id,idempotency_key,"
            "create_fingerprint,created_at) VALUES (%s,%s,%s,%s,%s,%s,now())",
            [
                data["enrollment"],
                staff["organization"],
                staff["clinic_a"],
                data["patient"],
                str(uuid4()),
                secrets.token_bytes(32),
            ],
        )
        _grant(conn, staff, data, operations, data["code"])
        if day is not None:
            conn.execute(
                "INSERT INTO clinic_app.scheduling_availabilityblock "
                "(id,organization_id,clinic_id,practitioner_id,start_at,end_at,"
                "idempotency_key,create_fingerprint,created_at,updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,now(),now())",
                [
                    str(uuid4()),
                    staff["organization"],
                    staff["clinic_a"],
                    staff["physician_a_id"],
                    day + " 11:00+00",
                    day + " 15:00+00",
                    str(uuid4()),
                    secrets.token_bytes(32),
                ],
            )
        if questionnaire:
            conn.execute(
                "INSERT INTO clinic_app.intake_questionnairetemplate "
                "(id,organization_id,clinic_id,key,version,title,questions,created_at)"
                " VALUES (%s,%s,%s,%s,1,%s,%s,now())",
                [
                    data["template"],
                    staff["organization"],
                    staff["clinic_a"],
                    data["template"],
                    "Pré-consulta sintética do portal",
                    Jsonb(QUESTIONS),
                ],
            )
            conn.execute(
                "INSERT INTO clinic_app.intake_questionnaireresponse "
                "(id,organization_id,clinic_id,patient_id,enrollment_id,template_id,"
                "state,revision,restricted_from_delegates,created_at,updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,'draft',1,true,now(),now())",
                [
                    data["response"],
                    staff["organization"],
                    staff["clinic_a"],
                    data["patient"],
                    data["enrollment"],
                    data["template"],
                ],
            )
    return data


def _grant(
    conn: psycopg.Connection[TupleRow],
    staff: dict[str, str],
    data: dict[str, str],
    operations: tuple[str, ...],
    code: str,
) -> None:
    conn.execute(
        "INSERT INTO clinic_app.intake_patientaccessgrant "
        "(id,organization_id,clinic_id,patient_id,enrollment_id,issued_by_id,"
        "issued_by_label,secret_hash,operations,expires_at,created_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,'Synthetic',%s,%s::varchar(32)[],"
        "now()+interval '24 hours',now())",
        [
            str(uuid4()),
            staff["organization"],
            staff["clinic_a"],
            data["patient"],
            data["enrollment"],
            staff["receptionist_id"],
            hashlib.sha256(code.encode()).digest(),
            list(operations),
        ],
    )


def _reissue(staff: dict[str, str], data: dict[str, str]) -> str:
    code = new_access_code()
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant', %s, true)", [staff["organization"]]
        )
        _grant(conn, staff, data, ("enrollment_view", "questionnaires"), code)
    return code


def _expire_sessions(staff: dict[str, str], patient_id: str) -> None:
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant',%s,true)", [staff["organization"]]
        )
        conn.execute(
            "UPDATE clinic_app.intake_patientsession "
            "SET created_at=now()-interval '9 hours', "
            "expires_at=now()-interval '1 minute' WHERE patient_id=%s",
            [patient_id],
        )


def _context(
    browser: Browser,
    width: int,
    *,
    forced_colors: Literal["active", "none"] = "none",
    java_script_enabled: bool = True,
    video: Path | None = None,
) -> BrowserContext:
    return new_context(
        browser,
        locale="pt-BR",
        timezone_id="Asia/Tokyo",
        viewport={"width": width, "height": 900},
        reduced_motion="reduce",
        forced_colors=forced_colors,
        java_script_enabled=java_script_enabled,
        record_video_dir=None if video is None else str(video),
        record_video_size=None if video is None else {"width": width, "height": 900},
    )


def _capture(page: Page, root: Path, state: str) -> None:
    folder = root / "portal"
    folder.mkdir(exist_ok=True, mode=0o700)
    full_page_screenshot(page, folder / f"{state}.png")
    assert _no_overflow(page), _overflowing(page)


def _small_targets(page: Page) -> list[list[object]]:
    return [
        target
        for target in evaluate_js(page, TARGETS_JS)
        if int(target[1]) < MIN_TARGET_PX or int(target[2]) < MIN_TARGET_PX
    ]


def _axe_clean(page: Page, base: str, root: Path, state: str) -> None:
    report = check_page(page, base, root, f"portal-{state}")
    assert report["violations"] == 0, report


def _destinations(page: Page) -> list[str]:
    return [
        str(key)
        for key in page.locator("[data-portal-destination]").evaluate_all(
            "(items) => items.map((item) => item.dataset.portalDestination)"
        )
    ]


def _no_browser_storage(page: Page) -> None:
    """No clinical text can persist: no storage, cache, worker or push permission.

    The shared shell's htmx keeps only the current path for its history, and a
    portal path carries no record selector (selectors travel in POST bodies).
    """
    state = page.evaluate(STORAGE_JS)
    session = state.pop("session")
    assert state == {
        "local": 0,
        "caches": 0,
        "databases": 0,
        "workers": 0,
        "notifications": "default",
    }
    assert [key for key, _value in session] in ([], ["htmx-current-path-for-history"])
    assert all(
        value.startswith("/patient/") and "?" not in value for _key, value in session
    ), session


@pytest.mark.parametrize("width", MATRIX_WIDTHS)
def test_home_lists_only_granted_destinations_and_never_placeholders(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    width: int,
) -> None:
    staff = availability_staff
    full = _seed(
        staff, name=f"Paciente Sintético Portal {width}", operations=ALL_OPERATIONS
    )
    limited = _seed(
        staff,
        name=f"Paciente Sintético Restrito {width}",
        operations=("enrollment_view",),
    )
    browser = renewal_page.context.browser
    assert browser is not None
    with _context(browser, width) as context:
        page = context.new_page()
        errors = _watch_errors(page)
        _redeem(page, renewal_base_url, staff["clinic_a"], full["code"])
        expect(page.locator("[data-portal-home]")).to_be_visible()
        expect(page.locator("h1")).to_have_text(gettext("Your enrollment"))
        expect(page.locator("#patient-name")).to_have_text(
            f"Paciente Sintético Portal {width}"
        )
        assert _destinations(page) == list(DESTINATION_ORDER)
        expect(page.locator('[data-portal-destination="messages"]')).to_have_count(0)
        expect(page.locator("[data-portal-help]")).to_be_visible()
        expect(page.locator("[data-portal-delegate]")).to_have_count(0)
        assert _small_targets(page) == []
        _axe_clean(page, renewal_base_url, renewal_artifact_root, f"home-{width}")
        _capture(page, renewal_artifact_root, f"home-{width}")
        _no_browser_storage(page)
        click_to_navigate(page.locator("#teleconsult-link"))
        assert page.url.endswith("/patient/teleconsult/")
        assert errors == []
    with _context(browser, width) as context:
        page = context.new_page()
        _redeem(page, renewal_base_url, staff["clinic_a"], limited["code"])
        expect(page.locator("[data-portal-home]")).to_be_visible()
        assert _destinations(page) == list(LIMITED_ORDER)
        expect(page.locator("[data-portal-empty]")).to_have_count(0)
        for name in (
            "Suas consultas",
            "Suas cobranças",
            "Teleconsulta",
            "Seus registros",
        ):
            expect(page.get_by_role("link", name=name)).to_have_count(0)
        _axe_clean(page, renewal_base_url, renewal_artifact_root, f"limited-{width}")
        _capture(page, renewal_artifact_root, f"limited-{width}")


def test_summaries_follow_booking_and_released_synthetic_charge(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
) -> None:
    staff = availability_staff
    base = renewal_base_url
    day = "2035-07-03"
    data = _seed(
        staff,
        name="Paciente Sintético Resumo",
        operations=ALL_OPERATIONS,
        day=day,
        questionnaire=True,
    )
    manager = seed_manager(staff)
    browser = renewal_page.context.browser
    assert browser is not None
    # The patient journey (book, then open the released synthetic charge) is
    # recorded for the todo 50 QA artifact; the clip holds synthetic data only.
    video = renewal_artifact_root / "portal" / "video"
    with (
        _context(browser, 375, video=video) as patient_context,
        _context(browser, 1280) as staff_context,
    ):
        patient = patient_context.new_page()
        admin = staff_context.new_page()
        errors = _watch_errors(patient)
        _redeem(patient, base, staff["clinic_a"], data["code"])
        expect(patient.locator("[data-portal-next]")).to_have_count(0)
        expect(patient.locator('[data-pending="questionnaires"] .badge')).to_have_text(
            "1"
        )
        expect(patient.locator('[data-pending="charges"]')).to_have_count(0)
        _capture(patient, renewal_artifact_root, "summary-before")

        click_to_navigate(patient.locator("#appointments-link"))
        _choose_day(patient, day)
        click_to_navigate(patient.locator("[data-slot] button").first)
        expect(patient.locator("[data-appointment]")).to_have_count(1)
        goto_settled(patient, f"{base}/patient/")
        next_visit = patient.locator("[data-portal-next] time")
        expect(next_visit).to_have_attribute("datetime", f"{day}T08:00")
        expect(
            patient.locator("[data-portal-next] [data-appointment-status]")
        ).to_have_attribute("data-appointment-status", "scheduled")

        sign_in_manager(admin, base, staff, manager)
        charge_url = create_charge(
            admin, ledger_url(base, staff), data["patient"], AMOUNT
        )
        issue_and_code(admin)
        press(admin, "release")
        expect(admin.locator("[data-released]")).to_have_attribute(
            "data-released", "true"
        )

        goto_settled(patient, f"{base}/patient/")
        expect(patient.locator('[data-pending="charges"] .badge')).to_have_text("1")
        _axe_clean(patient, base, renewal_artifact_root, "summary-after")
        _capture(patient, renewal_artifact_root, "summary-after")
        click_to_navigate(patient.locator('[data-pending="charges"] a'))
        click_to_navigate(patient.locator("[data-charge] a"))
        back = patient.locator("[data-portal-back-link]")
        expect(back).to_have_attribute("href", "/patient/charges/")
        expect(back).to_have_text(gettext("Back to your charges"))
        _expect_support(patient)
        expect_state(patient, "pending")
        assert str(patient.locator("[data-code]").text_content()).startswith(
            CODE_PREFIX
        )
        _capture(patient, renewal_artifact_root, "summary-instructions")
        _attested_receipt(admin, patient, charge_url, renewal_artifact_root)
        goto_settled(patient, f"{base}/patient/")
        expect(patient.locator('[data-pending="charges"]')).to_have_count(0)
        assert errors == []


def _attested_receipt(admin: Page, patient: Page, charge_url: str, root: Path) -> None:
    """Only a stored, attested settlement turns the open charge to paid.

    The synthetic rehearsal ends with a receipt that never reads as money.
    """
    patient_charge_url = patient.url
    goto_settled(admin, charge_url)
    confirm_settlement(admin, AMOUNT)
    expect_state(admin, "paid")
    goto_settled(patient, patient_charge_url)
    expect_state(patient, "paid")
    expect(patient.locator("[data-receipt] [data-synthetic-receipt]")).to_be_visible()
    assert CODE_PREFIX not in str(patient.content())
    _capture(patient, root, "summary-receipt")


def _expect_support_panel(page: Page) -> None:
    support = page.locator("#portal-help[data-portal-help]")
    expect(support).to_have_count(1)
    expect(support).to_be_visible()


def _expect_support(page: Page) -> None:
    link = page.locator("[data-portal-back] [data-portal-help-link]")
    expect(link).to_have_count(1)
    expect(link).to_have_attribute("href", "#portal-help")
    _expect_support_panel(page)


def _expect_one_way_back(page: Page, href: str) -> None:
    back = page.locator("[data-portal-back] [data-portal-back-link]")
    expect(back).to_have_count(1)
    expect(back).to_have_attribute("href", href)
    expect(page.locator(".eyebrow")).to_have_count(0)
    _expect_support(page)


@pytest.mark.parametrize("width", DESTINATION_WIDTHS)
def test_every_destination_opens_with_one_way_back_to_the_portal(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    width: int,
) -> None:
    staff = availability_staff
    base = renewal_base_url
    data = _seed(
        staff, name=f"Paciente Sintético Destinos {width}", operations=ALL_OPERATIONS
    )
    browser = renewal_page.context.browser
    assert browser is not None
    with _context(browser, width) as context:
        page = context.new_page()
        errors = _watch_errors(page)
        _redeem(page, base, staff["clinic_a"], data["code"])
        for key in DESTINATION_ORDER:
            click_to_navigate(page.locator(f"#{key}-link"))
            _expect_one_way_back(page, HOME_PATH)
            expect(
                page.get_by_role("link", name=gettext("Back to the portal"))
            ).to_have_count(1)
            assert _small_targets(page) == []
            _axe_clean(page, base, renewal_artifact_root, f"{key}-{width}")
            _capture(page, renewal_artifact_root, f"{key}-{width}")
            _no_browser_storage(page)
            click_to_navigate(page.locator("[data-portal-back-link]"))
            expect(page.locator("[data-portal-home]")).to_be_visible()
        click_to_navigate(page.locator("#appointments-link"))
        click_to_navigate(page.get_by_role("link", name="Suas ofertas de horário"))
        _expect_one_way_back(page, HOME_PATH)
        assert _small_targets(page) == []
        _axe_clean(page, base, renewal_artifact_root, f"offers-{width}")
        _capture(page, renewal_artifact_root, f"offers-{width}")
        assert errors == []


def test_expired_session_keeps_the_saved_draft_and_explains_reentry(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
) -> None:
    staff = availability_staff
    base = renewal_base_url
    data = _seed(
        staff,
        name="Paciente Sintético Rascunho",
        operations=("enrollment_view", "questionnaires"),
        questionnaire=True,
    )
    browser = renewal_page.context.browser
    assert browser is not None
    with _context(browser, 375) as context:
        page = context.new_page()
        _redeem(page, base, staff["clinic_a"], data["code"])
        click_to_navigate(page.locator("#questionnaires-link"))
        press(page, "open")
        page.locator("#id_q_text").fill(DRAFT)
        press(page, "save")
        expect(page.locator("#id_q_text")).to_have_value(DRAFT)
        _expire_sessions(staff, data["patient"])
        page.locator("#id_q_text").fill(DRAFT + " não salvo")
        with page.expect_response(lambda r: r.request.method == "POST") as denied:
            press(page, "save")
        assert denied.value.status == FORBIDDEN
        expect(page.locator('[data-patient-gate="required"]')).to_be_visible()
        expect(page.locator("[data-gate-drafts]")).to_be_visible()
        _expect_support_panel(page)
        assert DRAFT not in page.content()
        _axe_clean(page, base, renewal_artifact_root, "gate-expired")
        _capture(page, renewal_artifact_root, "gate-expired")

        _redeem(page, base, staff["clinic_a"], _reissue(staff, data))
        click_to_navigate(page.locator("#questionnaires-link"))
        press(page, "open")
        expect(page.locator("#id_q_text")).to_have_value(DRAFT)
        _capture(page, renewal_artifact_root, "draft-recovered")
        _no_browser_storage(page)


def test_reflow_zoom_forced_colors_keyboard_and_native_baseline(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
) -> None:
    staff = availability_staff
    base = renewal_base_url
    browser = renewal_page.context.browser
    assert browser is not None
    data = _seed(staff, name="Paciente Sintético Matriz", operations=ALL_OPERATIONS)
    with _context(browser, 320, forced_colors="active") as context:
        page = context.new_page()
        _redeem(page, base, staff["clinic_a"], data["code"])
        _capture(page, renewal_artifact_root, "reflow-320-forced-colors")
        focused: list[str] = []
        for key in DESTINATION_ORDER:
            _tab_until(
                page,
                page.locator(f'[data-portal-destination="{key}"] a'),
                focused,
            )
        _tab_until(page, page.get_by_role("button", name=gettext("Sign out")), focused)
        zoom_context, zoomed = zoom_200(page)
        with zoom_context:
            goto_settled(zoomed, f"{base}/patient/")
            assert _no_overflow(zoomed), _overflowing(zoomed)
            _capture(zoomed, renewal_artifact_root, "zoom-200")
    native = _seed(staff, name="Paciente Sintético Nativo", operations=ALL_OPERATIONS)
    with _context(browser, 375, java_script_enabled=False) as context:
        page = context.new_page()
        _redeem(page, base, staff["clinic_a"], native["code"])
        assert _destinations(page) == list(DESTINATION_ORDER)
        click_to_navigate(page.get_by_role("button", name=gettext("Sign out")))
        expect(page.locator('[data-patient-gate="signed_out"]')).to_be_visible()
        _expect_support_panel(page)
        _capture(page, renewal_artifact_root, "native-signed-out")


@pytest.mark.parametrize("profile", ["iphone-15", "pixel-8"])
def test_data_rights_are_filed_and_the_copy_downloads_on_a_phone(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    profile: str,
) -> None:
    staff = availability_staff
    base = renewal_base_url
    data = _seed(
        staff, name=f"Paciente Sintético Direitos {profile}", operations=ALL_OPERATIONS
    )
    browser = renewal_page.context.browser
    assert browser is not None
    with mobile_context(browser, profile) as context:
        page = context.new_page()
        errors = _watch_errors(page)
        _redeem(page, base, staff["clinic_a"], data["code"])
        click_to_navigate(page.locator("#data-requests-link"))
        _expect_one_way_back(page, HOME_PATH)
        forms = page.locator("[data-data-request-form]")
        expect(forms).to_have_count(5)
        expect(
            page.locator("[data-data-sharing] [data-sharing-live=true]")
        ).to_have_count(0)
        _axe_clean(page, base, renewal_artifact_root, f"data-rights-{profile}")
        _capture(page, renewal_artifact_root, f"data-rights-{profile}")
        page.locator("#request-details").fill(CORRECTION)
        click_to_navigate(
            page.locator('[data-data-request-form="correction"] button[type=submit]')
        )
        expect(page.locator('[data-data-request-state="received"]')).to_have_count(1)
        expect(page.locator(".feedback--success")).to_be_visible()
        assert CORRECTION not in page.content()
        click_to_navigate(
            page.locator('[data-data-request-form="access"] button[type=submit]')
        )
        expect(page.locator('[data-data-request-state="received"]')).to_have_count(2)
        export = page.locator("[data-data-request-export]")
        expect(export).to_have_count(1)
        with page.expect_download() as received:
            export.click()
        assert received.value.suggested_filename.startswith("meus-dados-")
        assert received.value.failure() is None
        assert _small_targets(page) == []
        _axe_clean(page, base, renewal_artifact_root, f"data-rights-filed-{profile}")
        _capture(page, renewal_artifact_root, f"data-rights-filed-{profile}")
        _no_browser_storage(page)
        assert errors == []


def test_management_answers_a_filed_request_from_the_staff_queue(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
) -> None:
    staff = availability_staff
    base = renewal_base_url
    data = _seed(staff, name="Paciente Sintético Pedido", operations=ALL_OPERATIONS)
    # The suite shares one clinic, so this correction names its own row.
    correction = f"{CORRECTION} Pedido {uuid4().hex[:8]}"
    manager = seed_manager(staff)
    browser = renewal_page.context.browser
    assert browser is not None
    with (
        _context(browser, 375) as patient_context,
        _context(browser, 1280) as staff_context,
    ):
        patient = patient_context.new_page()
        admin = staff_context.new_page()
        errors = _watch_errors(admin)
        _redeem(patient, base, staff["clinic_a"], data["code"])
        click_to_navigate(patient.locator("#data-requests-link"))
        patient.locator("#request-details").fill(correction)
        click_to_navigate(
            patient.locator('[data-data-request-form="correction"] button[type=submit]')
        )
        expect(patient.locator('[data-data-request-state="received"]')).to_have_count(1)

        sign_in_manager(admin, base, staff, manager)
        goto_settled(admin, f"{base}/intake/clinics/{staff['clinic_a']}/data-requests/")
        row = admin.locator("[data-staff-request]").filter(
            has=admin.locator("[data-staff-request-details]", has_text=correction)
        )
        expect(row).to_have_count(1)
        expect(row).to_have_attribute("data-staff-request-state", "received")
        _axe_clean(admin, base, renewal_artifact_root, "staff-queue")
        _capture(admin, renewal_artifact_root, "staff-queue")
        click_to_navigate(row.locator('button[value="start"]'))
        expect(row).to_have_attribute("data-staff-request-state", "in_progress")
        click_to_navigate(row.locator('button[value="fulfill"]'))
        expect(row).to_have_attribute("data-staff-request-state", "fulfilled")
        _axe_clean(admin, base, renewal_artifact_root, "staff-queue-answered")
        _capture(admin, renewal_artifact_root, "staff-queue-answered")

        goto_settled(patient, f"{base}/patient/data-requests/")
        answered = patient.locator('[data-data-request-state="fulfilled"]')
        expect(answered).to_have_count(1)
        assert correction not in patient.content()
        assert errors == []


def test_display_preferences_stay_server_side_and_push_stays_unavailable(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
) -> None:
    staff = availability_staff
    base = renewal_base_url
    data = _seed(staff, name="Paciente Sintético Exibição", operations=ALL_OPERATIONS)
    browser = renewal_page.context.browser
    assert browser is not None
    with _context(browser, 375) as context:
        page = context.new_page()
        errors = _watch_errors(page)
        _redeem(page, base, staff["clinic_a"], data["code"])
        standard = page.evaluate(FONT_SIZE_JS)
        click_to_navigate(page.locator("#preferences-link"))
        expect(page.locator("[data-push-unavailable]")).to_be_visible()
        _axe_clean(page, base, renewal_artifact_root, "preferences")
        _capture(page, renewal_artifact_root, "preferences")
        page.locator("#text-size-large").check()
        page.locator("#contrast-high").check()
        click_to_navigate(page.locator("[data-display-save]"))
        expect(page.locator(".feedback--success")).to_be_visible()
        assert page.evaluate(FONT_SIZE_JS) != standard
        _axe_clean(page, base, renewal_artifact_root, "preferences-large-contrast")
        _capture(page, renewal_artifact_root, "preferences-large-contrast")
        click_to_navigate(page.locator("[data-portal-back-link]"))
        expect(page.locator("[data-portal-home]")).to_be_visible()
        assert page.evaluate(FONT_SIZE_JS) != standard
        assert _small_targets(page) == []
        _axe_clean(page, base, renewal_artifact_root, "home-large-contrast")
        _capture(page, renewal_artifact_root, "home-large-contrast")
        _no_browser_storage(page)
        assert errors == []
