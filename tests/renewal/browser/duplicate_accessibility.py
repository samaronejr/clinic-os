"""Real duplicate review, errors, merge and reversal through native forms."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

import pytest
from django.utils.translation import gettext
from playwright.sync_api import expect

from renewal.browser._navigation import (
    click_to_navigate,
    expect_document,
    goto_settled,
)
from renewal.browser._onboarding import sign_out
from renewal.browser._page_wait import await_autofocus
from renewal.browser._viewport_capture import capture_viewport
from renewal.browser.a11y_support import check_page
from renewal.browser.engines import element_box, mobile_context, new_context
from renewal.browser.test_availability import (
    _sign_in_physician,
    _sign_in_receptionist,
)
from renewal.browser.test_patient_access import _tab_until

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import Locator, Page, ViewportSize

    from renewal.browser._viewport_capture import Position


@dataclass(frozen=True, slots=True)
class DuplicateScene:
    """Equivalent reflow uses a 1280px reference divided by the named zoom."""

    name: str
    width: int
    height: int
    scale: int = 1
    reduced_motion: bool = False
    theme: Literal["light", "dark"] = "light"


SCENES = (
    DuplicateScene("phone", 390, 900, 3),
    DuplicateScene("desktop", 1440, 900),
    DuplicateScene("wide", 1920, 900),
    DuplicateScene("reflow-200", 640, 450, 2),
    DuplicateScene("reflow-400", 320, 225, 4),
    DuplicateScene("reduced-motion", 1440, 900, reduced_motion=True),
)
FIELD_LABELS = (
    "Legal name",
    "Social name",
    "Preferred name",
    "Sex at birth",
    "Gender identity",
    "Pronouns",
    "Preferred language",
    "Accessibility needs",
    "Occupation",
)


def _keyboard_submit(page: Page, button: Locator, focused: list[str]) -> int:
    _tab_until(page, button, focused, limit=80)
    with expect_document(page) as navigation:
        page.keyboard.press("Enter")
    return navigation.value.status


def _register_pair(
    page: Page, base: str, staff: dict[str, str], birth_date: str
) -> tuple[str, str]:
    """Seed through real clinic_app HTTP, not owner-created duplicate candidates."""
    _sign_in_receptionist(page, base, staff)
    token = uuid4().hex
    names = tuple(
        f"Synthetic {token} duplicate {suffix}" for suffix in ("Alpha", "Beta")
    )
    for name in names:
        goto_settled(page, f"{base}/intake/clinics/{staff['clinic_a']}/patients/new/")
        page.locator("#id_full_name").fill(name)
        page.locator("#id_birth_date").fill(birth_date)
        click_to_navigate(page.get_by_role("button", name=gettext("Register patient")))
        expect(page.locator("#patient-create-panel .feedback--error")).to_have_count(0)
    return names[0], names[1]


def _observe(
    page: Page,
    base: str,
    root: Path,
    scene: DuplicateScene,
    state: str,
) -> dict[str, object]:
    """Keep native axe findings, every target size, and validated top/end rasters."""
    stem = f"{scene.name}-{scene.theme}-{state}"
    size: ViewportSize = {"width": scene.width, "height": scene.height}
    captures = []
    positions: tuple[Position, Position] = ("top", "end")
    for position in positions:
        destination = root / f"{stem}-{position}.png"
        proof = capture_viewport(
            page,
            destination,
            theme=scene.theme,
            size=size,
            position=position,
        )
        captures.append({"path": destination.name, "proof": asdict(proof)})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    targets = []
    for button in page.locator("main button").all():
        if button.is_visible():
            box = element_box(button)
            assert box["height"] >= 44
            assert box["width"] >= 44
            targets.append({"width": box["width"], "height": box["height"]})
    motion = page.locator("button").first.evaluate(
        "(button) => getComputedStyle(button).transitionDuration"
    )
    if scene.reduced_motion:
        assert motion == "0s"
    return {
        "state": state,
        "axe": check_page(page, base, root, stem),
        "captures": captures,
        "button_targets": targets,
        "horizontal_overflow": False,
        "transition_duration": motion,
        "device_pixel_ratio": page.evaluate("devicePixelRatio"),
    }


def _fill_review_fields(page: Page, focused: list[str]) -> None:
    """Traverse and choose each real control with the keyboard, in DOM order."""
    for msgid in FIELD_LABELS:
        assert gettext(msgid) != msgid
        expect(page.get_by_label(f"{gettext(msgid)}:", exact=True)).to_be_visible()
    for control in page.locator(
        'select[name="survivor_patient_id"], select[name^="choice_"], #id_reason'
    ).all():
        _tab_until(page, control, focused, limit=80)
        if control.get_attribute("name") == "reason":
            page.keyboard.insert_text("Synthetic keyboard identity review")
        else:
            page.keyboard.press("Home")
            page.keyboard.press("ArrowDown")
        assert control.input_value()


@pytest.mark.parametrize(
    "scene",
    [replace(scene, theme=theme) for scene in SCENES for theme in ("light", "dark")],
    ids=lambda scene: f"{scene.name}-{scene.theme}",
)
def test_duplicate_review_merge_reversal_accessibility(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    scene: DuplicateScene,
) -> None:
    """Run the touched surface, not an EHR accessibility proxy."""
    browser = renewal_page.context.browser
    assert browser is not None
    context = (
        mobile_context(browser, "iphone-15")
        if scene.name == "phone"
        else new_context(
            browser,
            locale="pt-BR",
            viewport={"width": scene.width, "height": scene.height},
            device_scale_factor=scene.scale,
            reduced_motion="reduce" if scene.reduced_motion else "no-preference",
        )
    )
    root = renewal_artifact_root / "duplicates"
    root.mkdir(mode=0o700, exist_ok=True)
    page = context.new_page()
    focused: list[str] = []
    statuses: list[int] = []
    errors: list[str] = []
    states: list[dict[str, object]] = []
    page.on("pageerror", lambda error: errors.append(type(error).__name__))
    try:
        # Given: native registration creates the real deterministic candidate.
        year = (
            1970
            + SCENES.index(replace(scene, theme="light"))
            + (10 if scene.theme == "dark" else 0)
        )
        names = _register_pair(
            page, renewal_base_url, availability_staff, f"{year}-01-02"
        )
        sign_out(page, renewal_base_url)
        _sign_in_physician(page, renewal_base_url, availability_staff)
        url = (
            f"{renewal_base_url}/intake/clinics/"
            f"{availability_staff['clinic_a']}/patients/duplicates/"
        )
        goto_settled(page, url)
        pair = (
            page.get_by_role("row").filter(has_text=names[0]).filter(has_text=names[1])
        )
        statuses.append(_keyboard_submit(page, pair.get_by_role("button"), focused))
        states.append(_observe(page, renewal_base_url, root, scene, "review"))

        # When: keyboard-only review reaches validation, merge and both reversal paths.
        statuses.append(
            _keyboard_submit(page, page.locator('button[value="merge"]'), focused)
        )
        await_autofocus(page.locator("#duplicate-errors"))
        states.append(_observe(page, renewal_base_url, root, scene, "merge-error"))
        _fill_review_fields(page, focused)
        statuses.append(
            _keyboard_submit(page, page.locator('button[value="merge"]'), focused)
        )
        await_autofocus(page.locator(".feedback--success"))
        for msgid in ("Appointments", "Encounters", "Charges"):
            assert gettext(msgid) != msgid
            expect(page.get_by_text(gettext(msgid), exact=True)).to_be_visible()
        states.append(_observe(page, renewal_base_url, root, scene, "merged"))
        statuses.append(
            _keyboard_submit(page, page.locator('button[value="unmerge"]'), focused)
        )
        await_autofocus(page.locator("#duplicate-errors"))
        expect(page.locator("#unmerge-reason")).to_have_attribute(
            "aria-invalid", "true"
        )
        expect(page.locator("#unmerge-reason_error")).to_be_visible()
        states.append(_observe(page, renewal_base_url, root, scene, "reversal-error"))
        _tab_until(page, page.locator("#unmerge-reason"), focused, limit=80)
        page.keyboard.insert_text("Synthetic keyboard reversal review")
        statuses.append(
            _keyboard_submit(page, page.locator('button[value="unmerge"]'), focused)
        )
        await_autofocus(page.locator(".feedback--success"))
        expect(page.locator('button[value="unmerge"]')).to_have_count(0)
        states.append(_observe(page, renewal_base_url, root, scene, "separated"))

        # Then: the actual scenes and submissions, not planned checks, form the receipt.
        assert statuses == [200] * 5
        assert not errors
        destination = root / f"{scene.name}-{scene.theme}-report.json"
        destination.write_text(
            json.dumps(
                {
                    "scene": asdict(scene),
                    "theme": scene.theme,
                    "theme_mode": "production-attribute-forced-for-capture",
                    "runtime_role": "clinic_app",
                    "submissions": statuses,
                    "keyboard_stops": focused,
                    "states": states,
                    "page_errors": errors,
                    "source_binding": "../../report.json",
                    "reflow_model": (
                        "1280px reference divided by zoom; matched DPR"
                        if scene.name.startswith("reflow-")
                        else "native viewport and DPR"
                    ),
                    "logical_reversal": "alias deactivated; later bindings not moved",
                },
                sort_keys=True,
                indent=2,
            )
            + "\n"
        )
        destination.chmod(0o600)
    finally:
        context.close()
