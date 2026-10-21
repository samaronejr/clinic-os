"""Synthetic identity setup and native result commands on the real runtime."""

from __future__ import annotations

from typing import TYPE_CHECKING

import psycopg
from django.utils.translation import gettext as _
from playwright.sync_api import expect

from renewal.browser._protected import decrypt
from renewal.browser.engines import full_page_screenshot
from renewal.browser.test_patient_workspace import _patient

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import Locator, Page

SOURCE = b"%PDF-1.4\nsynthetic source result\n%%EOF"


def seed_result_patient(staff: dict[str, str], day: str) -> dict[str, str]:
    """Seed professional scope, then load independent source identity facts."""
    data = _patient(staff, day)
    with psycopg.connect(staff["dsn"]) as conn:
        conn.execute(
            "SELECT set_config('app.current_tenant',%s,true)", [staff["organization"]]
        )
        row = conn.execute(
            "SELECT full_name,birth_date FROM clinic_app.intake_patient WHERE id=%s",
            [data["patient"]],
        ).fetchone()
        assert row is not None
        name = decrypt(conn, "intake.patient.full_name", row[0])
        born = decrypt(conn, "intake.patient.birth_date", row[1])
        assert name is not None
        assert born is not None
        data.update(source_name=name.decode(), source_born=born.decode())
    return data


def submit(page: Page, button: Locator, message: str) -> None:
    """Observe the real POST and changed render for enhanced or native submission."""
    panel = page.locator("#results-workspace")
    before = panel.get_attribute("data-results-render")
    url = page.url
    with page.expect_response(
        lambda response: response.url == url and response.request.method == "POST"
    ) as posted:
        button.click()
    assert posted.value.status == 200
    expect(panel).not_to_have_attribute("data-results-render", before or "")
    expect(page.get_by_role("status").filter(has_text=_(message))).to_be_visible()


def place(page: Page, data: dict[str, str]) -> None:
    """Choose patient and catalog explicitly; never accept an implicit patient."""
    form = page.locator('form:has(input[name="action"][value="place"])')
    form.locator('[name="enrollment_id"]').select_option(data["enrollment"])
    form.locator('[name="exam_codes"]').select_option("cbc")
    submit(page, form.get_by_role("button"), "Order placed.")


def upload(page: Page, data: dict[str, str], *, wrong: bool = False) -> None:
    """Transcribe source identity rather than borrowing it from a chart default."""
    form = page.locator(
        'section[aria-labelledby="sources-heading"] '
        'form:has(input[name="action"][value="upload"])'
    ).first
    disclosure = form.locator("xpath=ancestor::details[1]")
    if disclosure.get_attribute("open") is None:
        disclosure.locator("summary").first.click()
    form.locator('[name="name"]').fill(
        "Pessoa SINTETICA diferente" if wrong else data["source_name"]
    )
    form.locator('[name="birth_date"]').fill(data["source_born"])
    form.locator('[name="source"]').set_input_files(
        {
            "name": "resultado-sintetico.pdf",
            "mimeType": "application/pdf",
            "buffer": SOURCE,
        }
    )
    form.locator('[name="value"]').fill("7,10")
    form.locator('[name="observation_code"]').select_option("cbc")
    form.locator('[name="unit"]').fill("unidade-da-fonte")
    form.locator('[name="reference_interval"]').fill("intervalo-da-fonte")
    form.locator('[name="abnormal_flag"]').select_option("high")
    submit(
        page,
        form.locator('button[type="submit"]'),
        "Result quarantined. The chart is unchanged; an operator task was created."
        if wrong
        else "Result matched. Clinical acknowledgment is still required.",
    )


def capture(page: Page, root: Path, state: str, width: int) -> None:
    """Retain the actual surface and reject horizontal page overflow."""
    directory = root / "results"
    directory.mkdir(mode=0o700, exist_ok=True)
    page.emulate_media(reduced_motion="reduce")
    full_page_screenshot(page, directory / f"{state}-{width}.png")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
