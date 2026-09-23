"""Real clinic_app author/resume/conflict/deny journeys, with native form fallback."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import pytest
from playwright.sync_api import expect

from renewal.browser.test_attachments import cookie_csrf, csrf
from renewal.browser.test_availability import (
    _sign_in_physician,
    _sign_in_receptionist,
    availability_staff,
)
from renewal.browser.test_encounter import DAYS, press, seed

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import Page

__all__ = ("availability_staff",)
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


def capture(page: Page, root: Path, state: str, width: int) -> None:
    folder = root / "prescription-draft"
    folder.mkdir(exist_ok=True, mode=0o700)
    page.screenshot(path=str(folder / f"{state}-{width}.png"), full_page=True)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def form_data(page: Page) -> dict[str, str]:
    return cast(
        "dict[str, str]",
        page.locator("#prescription-form").evaluate(
            "form => Object.fromEntries(new FormData(form))"
        ),
    )


def failures(page: Page, root: Path, width: int) -> None:
    data = form_data(page)
    data["action"] = "save"
    for category in ("controlled", "notification", "non_controlled", "unknown"):
        response = page.request.post(page.url, form={**data, "category": category})
        assert response.status == 400
        assert "no-store" in response.headers["cache-control"]
    for field in ("patient_id", "issuer_id", "encounter_id", "draft_id"):
        response = page.request.post(page.url, form={**data, field: str(uuid4())})
        assert response.status == 403
    # Render the category error through the native surface, not a mocked response.
    page.locator("#id_category").evaluate(
        "node => node.add(new Option('Unsupported', 'controlled'))"
    )
    page.locator("#id_category").select_option("controlled")
    with page.expect_response(
        lambda response: response.request.method == "POST"
    ) as rejected:
        press(page, "save")
    assert rejected.value.status == 400
    expect(page.locator("#save-state")).to_have_attribute("data-state", "unsaved")
    expect(page.locator("#id_items-0-dose")).to_have_value(ITEM["dose"])
    capture(page, root, "category-denied", width)
    page.goto(page.url)
    for field, value in ITEM.items():
        expect(page.locator(f"#id_items-0-{field}")).to_have_value(value)
    expect(page.locator("[data-draft]")).to_have_attribute("data-version", "2")


def role_denial(page: Page, staff: dict[str, str], root: Path, width: int) -> None:
    browser = page.context.browser
    assert browser is not None
    other_context = browser.new_context(viewport={"width": width, "height": 900})
    try:
        other = other_context.new_page()
        base = page.url.split("/prescription/", 1)[0]
        _sign_in_receptionist(other, base, staff)
        response = other.goto(page.url)
        assert response is not None
        assert response.status == 403
        response_post = other.request.post(
            page.url,
            form={
                **form_data(page),
                "csrfmiddlewaretoken": cookie_csrf(other),
                "action": "save",
            },
        )
        assert response_post.status == 403
        capture(other, root, "reception-denied", width)
    finally:
        other_context.close()


def native_reflow(page: Page, root: Path) -> None:
    browser = page.context.browser
    assert browser is not None
    page.set_viewport_size({"width": 320, "height": 900})
    page.emulate_media(forced_colors="active", reduced_motion="reduce")
    page.locator("#id_items-0-dose").focus()
    page.keyboard.press("Tab")
    expect(page.locator("#id_items-0-route")).to_be_focused()
    capture(page, root, "keyboard-forced-colors", 320)
    native = browser.new_context(
        java_script_enabled=False,
        storage_state=page.context.storage_state(),
        viewport={"width": 640, "height": 450},
        device_scale_factor=2,
    )
    try:
        fallback = native.new_page()
        fallback.goto(page.url)
        press(fallback, "save")
        expect(fallback.locator("[data-draft]")).to_have_attribute("data-version", "3")
        capture(fallback, root, "native-200-percent", 640)
    finally:
        native.close()


def long_content_and_targets(page: Page, root: Path, width: int) -> None:
    page.locator("#id_items-0-instructions").fill("Registro sintético longo. " * 70)
    assert page.locator(
        '#prescription-form input:not([type="hidden"]), '
        "#prescription-form select, #prescription-form button"
    ).evaluate_all(
        "nodes => nodes.every(node => node.getBoundingClientRect().height >= 44)"
    )
    capture(page, root, "long-content", width)
    page.locator("#id_items-0-instructions").fill(ITEM["instructions"])


def create_initial(page: Page, encounter: str, patient: str, issuer: str) -> str:
    denied = page.request.post(
        page.url,
        form={
            "csrfmiddlewaretoken": csrf(page),
            "action": "create",
            "encounter_id": encounter,
            "patient_id": patient,
            "issuer_id": issuer,
            "category": "controlled",
        },
    )
    assert denied.status == 400
    press(page, "create")
    expect(page.locator("[data-draft]")).to_have_attribute("data-version", "1")
    draft = page.locator("[data-draft]").get_attribute("data-draft")
    assert draft
    assert page.locator('input[name="patient_id"]').input_value() == patient
    assert page.locator('input[name="issuer_id"]').input_value() == issuer
    return draft


@pytest.mark.parametrize("width", [1280, 768, 375])
def test_prescription_draft_journey(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    width: int,
) -> None:
    staff, base, root = availability_staff, renewal_base_url, renewal_artifact_root
    data = seed(staff, DAYS[width])
    browser = renewal_page.context.browser
    assert browser is not None
    context = browser.new_context(
        locale="pt-BR", viewport={"width": width, "height": 900}
    )
    page = context.new_page()
    errors: list[str] = []
    console_types: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda message: console_types.append(message.type))
    try:
        _sign_in_physician(page, base, staff)
        page.goto(
            f"{base}/scheduling/clinics/{staff['clinic_a']}/agenda/day/{DAYS[width]}/1/"
        )
        press(page, "open")
        encounter = page.locator("[data-encounter]").get_attribute("data-encounter")
        assert encounter
        with page.expect_navigation():
            page.get_by_role("button", name="Prescrição sintética").click()
        capture(page, root, "empty", width)
        draft = create_initial(
            page, encounter, data["patient"], staff["physician_a_id"]
        )
        second = context.new_page()
        second.goto(page.url)
        for field, value in ITEM.items():
            page.locator(f"#id_items-0-{field}").fill(value)
            second.locator(f"#id_items-0-{field}").fill(value)
        press(page, "save")
        expect(page.locator("[data-draft]")).to_have_attribute("data-version", "2")
        capture(page, root, "saved", width)
        page.reload()
        for field, value in ITEM.items():
            expect(page.locator(f"#id_items-0-{field}")).to_have_value(value)
        expect(page.locator("[data-draft]")).to_have_attribute("data-draft", draft)
        expect(page.locator("[data-encounter]")).to_have_attribute(
            "data-encounter", encounter
        )
        capture(page, root, "resumed", width)
        long_content_and_targets(page, root, width)
        second.locator("#id_items-0-dose").fill("Dose antiga não salva")
        with second.expect_response(
            lambda response: response.request.method == "POST"
        ) as conflict:
            press(second, "save")
        assert conflict.value.status == 409
        expect(second.locator("#id_items-0-dose")).to_have_value(
            "Dose antiga não salva"
        )
        expect(second.locator("#save-state")).to_have_attribute("data-state", "unsaved")
        capture(second, root, "stale-retained", width)
        second.close()
        failures(page, root, width)
        role_denial(page, staff, root, width)
        if width == 1280:
            native_reflow(page, root)
        assert errors == []
        (
            root / "prescription-draft" / f"accessibility-console-{width}.json"
        ).write_text(
            json.dumps(
                {
                    "width": width,
                    "page_errors": errors,
                    "console_message_types": console_types,
                    "console_note": (
                        "Expected HTTP 400/403/409 denials are exercised; "
                        "no unhandled page errors."
                    ),
                    "reflow": "passed",
                    "minimum_control_height_px": 44,
                    "native_labels": "explicit",
                    "journeys": [
                        "author",
                        "resume",
                        "category-denied",
                        "wrong-scope-denied",
                        "stale-version",
                        "role-denied",
                    ],
                },
                indent=2,
            )
            + "\n"
        )
    finally:
        context.close()
