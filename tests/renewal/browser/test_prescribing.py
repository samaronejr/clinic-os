"""Real clinic_app prescribing journey: author, review, identity, sign, result.

The journey runs once per matrix width (1280, 768, 375) against the supervised
runtime served as ``clinic_app``; the recovery scene adds the failure states a
physician must be able to recover from: a genuinely concurrent double submit,
a provider-reported failure with its retry, and a draft that moved on after a
document was frozen. Every wait subscribes to a navigation, a response or a
DOM state, never to a timer. All content is synthetic.

The identity step is exercised here in its fresh state (the review screen
names the confirmed identity and its validity); the stale challenge and the
return to the same review are proven deterministically in
``tests/renewal/test_prescribing_workflow.py``, because a real browser cannot
age the server's step-up clock without sleeping.
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import secrets
import threading
from typing import TYPE_CHECKING
from urllib.parse import urlencode, urlsplit

import psycopg
import pytest
from playwright.sync_api import expect

from renewal.browser.test_availability import (
    _sign_in_physician,
    availability_staff,
)
from renewal.browser.test_document_verification import (
    SIGNATURE_HEADER,
    SYNTHETIC_SECRET,
    _document_row,
    _operation_row,
    _signed_callback,
)
from renewal.browser.test_encounter import press, press_in_view, seed

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import Page

__all__ = ("availability_staff",)

DAYS = {1280: "2035-08-11", 768: "2035-08-12", 375: "2035-08-13"}
RECOVERY_DAY = "2035-08-14"
PATIENT = "Paciente Sintético Questionário"
ITEM = {
    "medication_description": " Medicamento fictício — não utilizar ",
    "strength_form": "Concentração e forma sintéticas",
    "dose": " Dose sintética digitada  ",
    "route": "Via sintética",
    "frequency": "Frequência sintética",
    "duration": "Duração sintética",
    "quantity": "Quantidade sintética",
    "instructions": "Orientações explícitas do médico. Sem sugestões automáticas.",
}
CALLBACK_PATH = "/prescription/signing/callback/synthetic-signature-v1/"


def capture(page: Page, root: Path, state: str, width: int) -> None:
    folder = root / "prescribing"
    folder.mkdir(exist_ok=True, mode=0o700)
    destination = folder / f"{state}-{width}.png"
    page.screenshot(path=str(destination), full_page=True)
    destination.chmod(0o600)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def ensure_physician_profile(staff: dict[str, str]) -> None:
    """Provision the synthetic signing identity once, through the owner role."""
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant', %s, true)",
            [staff["organization"]],
        )
        conn.execute(
            "INSERT INTO clinic_app.identity_physicianprofile "
            "(id,organization_id,user_id,jurisdiction,registration_number,"
            "signing_subject,synthetic,status) "
            "VALUES (gen_random_uuid(),%s,%s,'SP','SYNTHETIC-CRM-36',%s,true,"
            "'unknown') ON CONFLICT DO NOTHING",
            [
                staff["organization"],
                staff["physician_a_id"],
                f"synthetic:physician:{staff['physician_a_id']}",
            ],
        )


def operations(staff: dict[str, str], document: str) -> list[tuple[str, str]]:
    """Return every stored attempt for one document, oldest first."""
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant', %s, true)",
            [staff["organization"]],
        )
        rows = conn.execute(
            "SELECT state, failure_reason FROM "
            "clinic_app.prescription_signatureoperation "
            "WHERE document_id = %s ORDER BY created_at",
            [document],
        ).fetchall()
    return [(str(row[0]), str(row[1])) for row in rows]


def failed_callback(
    staff: dict[str, str], document: str
) -> tuple[dict[str, str], bytes]:
    """Build the provider's authenticated failure report for the live attempt."""
    operation = _operation_row(staff, document)
    body = json.dumps(
        {
            "event_id": f"event-{secrets.token_hex(8)}",
            "operation_id": operation["operation_ref"],
            "status": "failed",
        }
    ).encode()
    return {
        SIGNATURE_HEADER: hmac.new(SYNTHETIC_SECRET, body, hashlib.sha256).hexdigest()
    }, body


def post_callback(
    page: Page, base: str, staff: dict[str, str], document: str, *, signed: bool = True
) -> None:
    headers, body = (
        _signed_callback(staff, document)
        if signed
        else failed_callback(staff, document)
    )
    response = page.request.post(
        f"{base}{CALLBACK_PATH}",
        data=body,
        headers={"Content-Type": "application/json", **headers},
    )
    assert response.status == 200


def author_and_render(page: Page, base: str, staff: dict[str, str], day: str) -> str:
    """Author one draft from the encounter and freeze it into a document."""
    page.goto(f"{base}/scheduling/clinics/{staff['clinic_a']}/agenda/day/{day}/1/")
    press(page, "open")
    with page.expect_navigation():
        page.get_by_role("button", name="Prescrição sintética").click()
    press(page, "create")
    for field, value in ITEM.items():
        page.locator(f"#id_items-0-{field}").fill(value)
    press(page, "save")
    press_in_view(page, "render_document")
    expect(page.locator("[data-step]")).to_have_attribute("data-step", "review")
    document = page.locator("[data-document]").first.get_attribute("data-document")
    assert document
    return document


def assert_subject(page: Page, staff: dict[str, str]) -> None:
    """The fixed subject is present and identical on every screen."""
    expect(page.locator('[data-context="patient"]')).to_contain_text(PATIENT)
    expect(page.locator('[data-context="issuer"]')).to_contain_text(
        staff["physician_a"]
    )
    expect(page.locator('[data-context="clinic"]')).to_contain_text("Clínica")


def press_once(page: Page, form: str, action: str, root: Path, width: int) -> None:
    """Prove the submit feedback: busy form, disabled button, progress text.

    The product's listener is delegated on ``document``; a listener registered
    afterwards on the same target runs second, so the feedback is applied and
    only then is the navigation cancelled. Nothing here is timing-based.
    """
    page.evaluate(
        "document.addEventListener('submit', e => e.preventDefault(), {once: true})"
    )
    button = page.locator(f"{form} button[value='{action}']")
    button.click()
    expect(page.locator(form)).to_have_attribute("aria-busy", "true")
    expect(button).to_be_disabled()
    expect(page.locator(f"{form} [data-once-status]")).to_be_visible()
    capture(page, root, "submit-feedback", width)
    # The disabled look is feedback only; the page reloads and still signs.
    page.reload()
    expect(page.locator(f"{form} button[value='{action}']")).to_be_enabled()


def concurrent_sign(page: Page, url: str) -> list[tuple[int, str]]:
    """Fire two genuinely parallel sign submits over the real HTTP boundary."""
    parts = urlsplit(url)
    token = page.locator('#sign-form input[name="csrfmiddlewaretoken"]').input_value()
    cookies = "; ".join(
        f"{cookie['name']}={cookie['value']}" for cookie in page.context.cookies()
    )
    body = urlencode({"csrfmiddlewaretoken": token, "action": "sign_document"})
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Cookie": cookies,
    }
    results: list[tuple[int, str]] = []
    guard = threading.Lock()
    start = threading.Barrier(2)

    def submit() -> None:
        connection = http.client.HTTPConnection(
            parts.hostname or "127.0.0.1", parts.port, timeout=30
        )
        try:
            start.wait(timeout=30)
            connection.request("POST", parts.path, body=body, headers=headers)
            response = connection.getresponse()
            response.read()
            with guard:
                results.append((response.status, response.headers.get("Location", "")))
        finally:
            connection.close()

    threads = [threading.Thread(target=submit) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not any(thread.is_alive() for thread in threads)
    assert len(results) == 2
    return results


def check_reflow(page: Page, root: Path, width: int) -> None:
    """320px reflow, forced colors, reduced motion, keyboard and 44px targets."""
    page.set_viewport_size({"width": 320, "height": 900})
    page.emulate_media(forced_colors="active", reduced_motion="reduce")
    capture(page, root, "forced-colors-reflow", 320)
    page.emulate_media(forced_colors="none", reduced_motion="no-preference")
    sign = page.locator('#sign-form button[value="sign_document"]')
    sign.focus()
    expect(sign).to_be_focused()
    capture(page, root, "keyboard-focus", 320)
    assert page.locator("#sign-form button").evaluate_all(
        "nodes => nodes.every(node => node.getBoundingClientRect().height >= 44)"
    )
    page.set_viewport_size({"width": width, "height": 900})


@pytest.mark.parametrize("width", [1280, 768, 375])
def test_prescribing_journey(  # noqa: PLR0915 - one linear clinician journey
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    width: int,
) -> None:
    staff, base, root = availability_staff, renewal_base_url, renewal_artifact_root
    seed(staff, DAYS[width])
    ensure_physician_profile(staff)
    browser = renewal_page.context.browser
    assert browser is not None
    context = browser.new_context(
        locale="pt-BR", viewport={"width": width, "height": 900}
    )
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    draft_url = f"{base}/prescription/clinics/{staff['clinic_a']}/draft/"
    try:
        _sign_in_physician(page, base, staff)
        page.goto(draft_url)
        capture(page, root, "author-empty", width)
        document = author_and_render(page, base, staff, DAYS[width])
        review_url = page.url

        # Review: the fixed subject, the exact frozen content, the identity.
        assert_subject(page, staff)
        expect(page.locator("[data-version]")).to_have_attribute("data-version", "2")
        expect(page.locator("[data-identity]")).to_have_attribute(
            "data-identity", "fresh"
        )
        expect(page.locator("[data-signature]")).to_have_attribute(
            "data-signature", "ready"
        )
        for field, value in ITEM.items():
            assert page.locator(f'[data-field="{field}"]').first.text_content() == value
        digest = page.locator("[data-digest]").get_attribute("data-digest")
        assert digest
        capture(page, root, "review", width)
        if width == 375:
            check_reflow(page, root, width)

        # A held submit shows busy feedback; the server is the real guard.
        press_once(page, "#sign-form", "sign_document", root, width)

        # Sign: explicit progress, nothing issued yet.
        press_in_view(page, "sign_document")
        signing_url = page.url
        expect(page.locator("[data-state]")).to_have_attribute("data-state", "signing")
        assert page.locator("[data-progress-state]").evaluate_all(
            "nodes => nodes.map(node => node.dataset.progressState)"
        ) == ["done", "done", "current"]
        assert_subject(page, staff)
        capture(page, root, "signing", width)

        # Result: verified rehearsal, named as synthetic, with its evidence.
        post_callback(page, base, staff, document)
        page.goto(signing_url)
        expect(page.locator("[data-state]")).to_have_attribute(
            "data-state", "rehearsal_complete"
        )
        expect(page.locator("[data-result]")).to_contain_text("sem validade")
        expect(page.locator('[data-evidence="signed_digest"]')).to_be_visible()
        expect(page.locator('[data-evidence="content_digest"]')).to_contain_text(digest)
        assert page.locator("[data-progress-state]").evaluate_all(
            "nodes => nodes.map(node => node.dataset.progressState)"
        ) == ["done", "done", "done"]
        assert page.locator('button[value="download_signed"]').is_visible()
        capture(page, root, "result", width)

        # Follow the actual result return link to the document's workspace.
        encounter = page.locator("[data-subject]").get_attribute("data-subject")
        assert encounter
        with page.expect_navigation():
            page.locator('a[href$="/draft/"]').click()
        expect(page.locator("[data-subject]")).to_have_attribute(
            "data-subject", encounter
        )
        row = page.locator(f'[data-document="{document}"]')
        expect(row).to_have_attribute("data-signature", "rehearsal_complete")
        expect(row).to_contain_text("Ensaio concluído")
        expect(row).to_contain_text(digest[:16])
        assert page.locator('button[value="release_document"]').is_visible()
        capture(page, root, "history", width)

        # The workspace return submits its own encounter through the real form.
        press(page, "show")
        expect(page.locator("[data-encounter]")).to_have_attribute(
            "data-encounter", encounter
        )
        capture(page, root, "returned-encounter", width)
        with page.expect_navigation():
            page.get_by_role("button", name="Prescrição sintética").click()
        expect(page.locator("[data-subject]")).to_have_attribute(
            "data-subject", encounter
        )

        # Discard removes editing, not immutable history or its evidence links.
        press_in_view(page, "discard")
        expect(row).to_have_attribute("data-signature", "rehearsal_complete")
        assert page.locator("#prescription-form").count() == 0
        assert page.locator('button[value="render_document"]').count() == 0
        expect(row.locator('button[value="release_document"]')).to_be_visible()
        capture(page, root, "history-discarded", width)
        with page.expect_navigation():
            row.locator('a[href*="/signing/"]').click()
        assert page.url == signing_url
        expect(page.locator('[data-evidence="content_digest"]')).to_contain_text(digest)
        with page.expect_download() as download:
            page.locator('button[value="download_signed"]').click()
        assert download.value.failure() is None
        capture(page, root, "result-after-discard", width)

        # Public verification: a synthetic status, never clinical content.
        handle = str(_document_row(staff, document)["handle"])
        anonymous = browser.new_context(
            locale="pt-BR", viewport={"width": width, "height": 900}
        )
        try:
            public = anonymous.new_page()
            response = public.goto(f"{base}/prescription/verify/{handle}/")
            assert response is not None
            assert response.status == 200
            # ``rehearsal_complete`` is the synthetic verdict; ``issued`` is
            # reserved for an approved real provider and is never reached here.
            expect(public.locator("#verify-status")).to_have_attribute(
                "data-status", "rehearsal_complete"
            )
            assert PATIENT not in public.content()
            assert ITEM["dose"].strip() not in public.content()
            capture(public, root, "public-verify", width)
        finally:
            anonymous.close()

        page.goto(review_url)
        expect(page.locator("[data-signature]")).to_have_attribute(
            "data-signature", "complete"
        )
        assert not page.locator('button[value="sign_document"]').count()
        assert errors == []
        (root / "prescribing" / f"report-{width}.json").write_text(
            json.dumps(
                {
                    "width": width,
                    "page_errors": errors,
                    "document": document,
                    "content_digest": digest,
                    "states": ["signing", "rehearsal_complete"],
                    "public_status": "rehearsal_complete",
                    "history_after_discard": True,
                    "signed_download_after_discard": True,
                    "returned_encounter": encounter,
                    "screens": ["author", "review", "sign", "result", "verify"],
                },
                indent=2,
            )
            + "\n"
        )
    finally:
        context.close()


def test_prescribing_recovery_double_submit_and_stale_draft(  # noqa: PLR0915 - one recovery scene
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
) -> None:
    staff, base, root = availability_staff, renewal_base_url, renewal_artifact_root
    seed(staff, RECOVERY_DAY)
    ensure_physician_profile(staff)
    browser = renewal_page.context.browser
    assert browser is not None
    context = browser.new_context(
        locale="pt-BR", viewport={"width": 1280, "height": 900}
    )
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    draft_url = f"{base}/prescription/clinics/{staff['clinic_a']}/draft/"
    try:
        _sign_in_physician(page, base, staff)
        document = author_and_render(page, base, staff, RECOVERY_DAY)
        review_url = page.url
        digest = page.locator("[data-digest]").get_attribute("data-digest")
        assert digest

        # Two genuinely parallel submits resolve to exactly one operation.
        results = concurrent_sign(page, review_url)
        assert [status for status, _ in results] == [302, 302]
        assert len({location for _, location in results}) == 1
        assert operations(staff, document) == [("signing", "")]
        signing_url = f"{base}{results[0][1]}"
        page.goto(signing_url)
        expect(page.locator("[data-state]")).to_have_attribute("data-state", "signing")
        capture(page, root, "double-submit-single-operation", 1280)

        # A provider-reported failure is terminal, named, and recoverable.
        post_callback(page, base, staff, document, signed=False)
        page.goto(signing_url)
        expect(page.locator("[data-state]")).to_have_attribute("data-state", "failed")
        expect(page.locator("[data-failure]")).to_have_attribute(
            "data-failure", "provider_reported"
        )
        assert page.locator("[data-progress-state]").evaluate_all(
            "nodes => nodes.map(node => node.dataset.progressState)"
        ) == ["done", "done", "failed"]
        assert not page.locator('button[value="download_signed"]').count()
        capture(page, root, "provider-failed", 1280)

        press(page, "restart_signature")
        retry_url = page.url
        assert retry_url != signing_url
        expect(page.locator("[data-state]")).to_have_attribute("data-state", "signing")
        expect(page.locator("[data-attempt-state]").first).to_have_attribute(
            "data-attempt-state", "failed"
        )
        capture(page, root, "retry", 1280)
        assert operations(staff, document) == [
            ("failed", "provider_reported"),
            ("signing", ""),
        ]

        post_callback(page, base, staff, document)
        page.goto(retry_url)
        expect(page.locator("[data-state]")).to_have_attribute(
            "data-state", "rehearsal_complete"
        )
        # The failed attempt is retained verbatim next to the completed one,
        # and its stale page points at the current attempt instead of offering
        # a restart that could only fail.
        page.goto(signing_url)
        expect(page.locator("[data-state]")).to_have_attribute("data-state", "failed")
        expect(page.locator('[data-evidence="content_digest"]')).to_contain_text(digest)
        expect(page.locator('button[value="restart_signature"]')).to_have_count(0)
        # The failure alert must not offer a retry either: the document's
        # signing state now lives on the newer attempt.
        failure_alert = page.locator("[data-failure-reason]")
        expect(failure_alert).to_be_visible()
        expect(failure_alert).not_to_contain_text("tentar novamente")
        superseded = page.locator("[data-superseded]")
        expect(superseded).to_be_visible()
        expect(
            page.get_by_role("link", name="Ver a tentativa atual")
        ).to_have_attribute("href", urlsplit(retry_url).path)
        assert operations(staff, document) == [
            ("failed", "provider_reported"),
            ("rehearsal_complete", ""),
        ]

        # A draft that moved on never rewrites the frozen document.
        stale = context.new_page()
        stale.goto(draft_url)
        page.goto(draft_url)
        page.locator("#id_items-0-dose").fill("Dose alterada depois da renderização")
        press_in_view(page, "save")
        expect(page.locator("[data-draft]")).to_have_attribute("data-version", "3")
        with stale.expect_response(
            lambda response: response.request.method == "POST"
        ) as conflict:
            press_in_view(stale, "render_document")
        assert conflict.value.status == 409
        expect(stale.locator("[data-draft]")).to_have_attribute("data-version", "3")
        expect(stale.locator("[data-step]")).to_have_attribute("data-step", "author")
        expect(stale.locator("[data-outcome]")).to_have_attribute(
            "data-outcome", "draft_moved"
        )
        capture(stale, root, "stale-render-conflict", 1280)
        stale.close()

        page.goto(review_url)
        expect(page.locator("[data-stale]")).to_have_attribute("data-stale", "true")
        assert page.locator('[data-field="dose"]').first.text_content() == ITEM["dose"]
        expect(page.locator("[data-digest]")).to_have_attribute("data-digest", digest)
        capture(page, root, "stale-review", 1280)

        # A precondition the physician can act on is named, not refused: the
        # released document has no verified patient destination yet.
        page.goto(draft_url)
        press_in_view(page, "release_document")
        with page.expect_response(
            lambda response: response.request.method == "POST"
        ) as denied:
            press_in_view(page, "deliver_document")
        assert denied.value.status == 409
        expect(page.locator("[data-outcome]")).to_have_attribute(
            "data-outcome", "no_verified_contact"
        )
        expect(page.locator(f'[data-document="{document}"]')).to_have_attribute(
            "data-signature", "rehearsal_complete"
        )
        capture(page, root, "delivery-precondition", 1280)
        assert errors == []
        (root / "prescribing" / "report-recovery.json").write_text(
            json.dumps(
                {
                    "page_errors": errors,
                    "document": document,
                    "concurrent_submits": 2,
                    "operations_after_concurrent_submits": 1,
                    "attempts": ["failed:provider_reported", "rehearsal_complete"],
                    "stale_render_http": 409,
                    "delivery_precondition": "no_verified_contact (409)",
                },
                indent=2,
            )
            + "\n"
        )
    finally:
        context.close()
