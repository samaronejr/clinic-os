"""Real-browser upload, review, release and wrong-patient quarantine journeys."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from django.utils.translation import gettext as _
from playwright.sync_api import expect

from renewal.browser._navigation import goto_settled
from renewal.browser.a11y_support import check_page
from renewal.browser.engines import element_box
from renewal.browser.results_lab_support import runtime_effects, seed_delivery
from renewal.browser.results_support import (
    SOURCE,
    capture,
    place,
    seed_result_patient,
    submit,
    upload,
)
from renewal.browser.test_availability import _sign_in_physician, availability_staff

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import Page

__all__ = ("availability_staff",)
DAYS = {1280: "2039-06-12", 768: "2039-06-13", 375: "2039-06-14"}


@pytest.mark.parametrize("width", [1280, 768, 375])
def test_result_upload_acknowledgment_release_and_wrong_patient(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    width: int,
) -> None:
    staff, root = availability_staff, renewal_artifact_root
    data = seed_result_patient(staff, DAYS[width])
    _sign_in_physician(renewal_page, renewal_base_url, staff)
    browser = renewal_page.context.browser
    assert browser is not None
    directory = root / "results"
    directory.mkdir(mode=0o700, exist_ok=True)
    context = browser.new_context(
        storage_state=renewal_page.context.storage_state(),
        locale="pt-BR",
        timezone_id="Asia/Tokyo",
        viewport={"width": width, "height": 900},
        record_video_dir=str(directory / "video") if width == 1280 else None,
    )
    page = context.new_page()
    try:
        url = f"{renewal_base_url}/clinics/{staff['clinic_a']}/results/"
        response = goto_settled(page, url)
        assert response is not None
        assert response.status == 200
        expect(page.get_by_role("heading", level=1)).to_have_text(
            _("Orders and results")
        )
        capture(page, root, "empty", width)
        place(page, data)
        upload(page, data)
        row = page.locator("article[data-result-state]").first
        expect(row).to_have_attribute("data-result-state", "matched")
        expect(page.get_by_role("table").first).to_contain_text("7,10")
        expect(row.get_by_role("heading", level=3)).to_have_text(data["source_name"])
        expect(
            row.get_by_role("button", name=_("Release this version to patient"))
        ).to_have_count(0)
        capture(page, root, "matched-not-reviewed", width)
        row.locator('[name="disposition"]').select_option("follow_up")
        submit(
            page,
            row.get_by_role("button", name=_("Acknowledge this version")),
            "Clinical acknowledgment recorded for this version.",
        )
        expect(row).to_have_attribute("data-result-state", "acknowledged")
        expect(page.locator("[data-results-outcome]")).to_be_focused()
        submit(
            page,
            row.get_by_role("button", name=_("Release this version to patient")),
            "This version was released to the selected audience.",
        )
        expect(row).to_have_attribute("data-result-state", "released")
        current = page.locator("article[data-result-state]").count()
        upload(page, data, wrong=True)
        expect(page.locator("article[data-result-state]")).to_have_count(current)
        capture(page, root, "wrong-patient-chart-unchanged", width)
        for theme in ("light", "dark"):
            page.locator("html").evaluate(
                "(node, theme) => { node.dataset.theme = theme; }", theme
            )
            capture(page, root, f"released-{theme}", width)
        check_page(page, renewal_base_url, root, f"results-{width}")
        for button in page.locator("main button:visible").all():
            bounds = element_box(button)
            assert bounds["height"] >= 44
            assert bounds["width"] >= 44
        page.emulate_media(forced_colors="active", reduced_motion="reduce")
        for reflow_width in (640, 320):
            page.set_viewport_size({"width": reflow_width, "height": 900})
            capture(page, root, f"reflow-from-{width}", reflow_width)
    finally:
        context.close()
        if page.video is not None:
            movie = directory / "result.webm"
            page.video.save_as(movie)
            movie.chmod(0o600)


def test_results_native_without_javascript(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
) -> None:
    staff = availability_staff
    data = seed_result_patient(staff, "2039-06-15")
    _sign_in_physician(renewal_page, renewal_base_url, staff)
    browser = renewal_page.context.browser
    assert browser is not None
    with browser.new_context(
        java_script_enabled=False,
        storage_state=renewal_page.context.storage_state(),
        locale="pt-BR",
        viewport={"width": 375, "height": 900},
    ) as context:
        page = context.new_page()
        goto_settled(page, f"{renewal_base_url}/clinics/{staff['clinic_a']}/results/")
        place(page, data)
        upload(page, data)
        expect(page.locator("article[data-result-state]").first).to_have_attribute(
            "data-result-state", "matched"
        )
        capture(page, renewal_artifact_root, "native-no-javascript", 375)


def test_signed_callback_http_replay_has_one_version_and_one_task(
    renewal_page: Page,
    renewal_base_url: str,
    availability_staff: dict[str, str],
) -> None:
    staff = availability_staff
    data = seed_result_patient(staff, "2039-06-16")
    delivery = seed_delivery(staff, data)
    url = f"{renewal_base_url}/orders/lab/callback/"
    first = renewal_page.request.post(url, data=delivery.body, headers=delivery.headers)
    replay = renewal_page.request.post(
        url, data=delivery.body, headers=delivery.headers
    )
    assert first.status == 200
    assert replay.status == 200
    assert first.json() == replay.json()
    assert first.json()["state"] == "matched"
    assert runtime_effects(staff, delivery.event) == (1, 1)


def test_results_errors_offline_keyboard_and_scope_refusal(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
) -> None:
    page, staff = renewal_page, availability_staff
    data = seed_result_patient(staff, "2039-06-17")
    _sign_in_physician(page, renewal_base_url, staff)
    url = f"{renewal_base_url}/clinics/{staff['clinic_a']}/results/"
    goto_settled(page, url)
    place(page, data)
    form = page.locator('section[aria-labelledby="sources-heading"] form').first
    summary = form.locator("xpath=ancestor::details[1]").locator("summary")
    summary.focus()
    summary.press("Enter")
    expect(form.locator('[name="name"]')).to_be_visible()
    event = form.locator('[name="event_id"]').input_value()
    form.locator('[name="name"]').fill(data["source_name"])
    form.locator('[name="birth_date"]').fill(data["source_born"])
    form.locator('[name="source"]').set_input_files(
        {
            "name": "resultado-sintetico.pdf",
            "mimeType": "application/pdf",
            "buffer": SOURCE,
        }
    )
    form.locator('[name="value"]').fill("7,10")
    with page.expect_response(
        lambda response: response.url == url and response.request.method == "POST"
    ) as invalid:
        form.locator('button[type="submit"]').click()
    assert invalid.value.status == 400
    error = page.locator("[data-focus-error]")
    expect(error).to_be_focused()
    expect(error).to_contain_text(
        _("Choose the requested examination for the transcribed value.")
    )
    capture(page, renewal_artifact_root, "invalid-examination", 1280)
    upload(page, data)
    # The same event with conflicting identity is a real replay conflict.
    form.locator('[name="event_id"]').evaluate(
        "(node, value) => { node.value = value; }", event
    )
    form.locator("xpath=ancestor::details[1]").locator("summary").click()
    form.locator('[name="name"]').fill("Pessoa SINTETICA diferente")
    form.locator('[name="birth_date"]').fill(data["source_born"])
    form.locator('[name="source"]').set_input_files(
        {
            "name": "resultado-sintetico.pdf",
            "mimeType": "application/pdf",
            "buffer": SOURCE,
        }
    )
    with page.expect_response(
        lambda response: response.url == url and response.request.method == "POST"
    ) as stale:
        form.locator('button[type="submit"]').click()
    assert stale.value.status == 409
    expect(page.locator("[data-focus-error]")).to_be_focused()
    capture(page, renewal_artifact_root, "conflict", 1280)
    row = page.locator("article[data-result-state]").first
    acknowledge = row.get_by_role("button", name=_("Acknowledge this version"))
    page.context.set_offline(True)
    expect(page.locator("[data-results-network]")).to_be_visible()
    expect(acknowledge).to_be_disabled()
    capture(page, renewal_artifact_root, "offline", 1280)
    page.context.set_offline(False)
    expect(acknowledge).to_be_enabled()
    row.locator('[name="disposition"]').select_option("no_action")
    row.locator('form:has(input[value="acknowledge"]) [name="version_id"]').evaluate(
        "(node, value) => { node.value = value; }", str(uuid4())
    )
    with page.expect_response(
        lambda response: response.url == url and response.request.method == "POST"
    ) as denied:
        acknowledge.click()
    assert denied.value.status == 403
    expect(page.locator("[data-results-denied]")).to_be_focused()
    expect(page.locator("article[data-result-state]")).to_have_count(0)
    capture(page, renewal_artifact_root, "scope-refused", 1280)
