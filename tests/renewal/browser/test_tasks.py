"""Task lifecycle, native fallback, signed bulk preview and inert denials."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from django.utils.translation import gettext
from playwright.sync_api import expect

from renewal.browser.a11y_support import check_page
from renewal.browser.engines import element_box, full_page_screenshot
from renewal.browser.test_availability import _sign_in_physician, availability_staff
from renewal.browser.test_retention import csrf, seed_manager, sign_in_manager

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import Locator, Page, Route

__all__ = ("availability_staff",)


def submit(page: Page, locator: Locator) -> None:
    if page.evaluate("typeof htmx !== 'undefined'"):
        with (
            page.expect_response(
                lambda response: response.request.method == "POST"
            ) as pending,
            page.expect_request_finished(lambda request: request.method == "POST"),
        ):
            locator.click()
        assert pending.value.status == 200
    else:
        with page.expect_navigation() as navigation:
            locator.click()
        response = navigation.value
        assert response is not None
        assert response.status == 200


def capture(page: Page, base: str, root: Path, state: str, width: int) -> None:
    folder = root / "tasks"
    folder.mkdir(mode=0o700, exist_ok=True)
    full_page_screenshot(page, folder / f"{state}-{width}.png")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    report = check_page(page, base, root, f"tasks-{state}-{width}")
    assert report["violations"] == 0
    for target in page.locator(
        "a, button, input:not([type=hidden]), select, textarea"
    ).all():
        if target.is_visible():
            box = element_box(target)
            assert box is not None
            assert box["width"] >= 44
            assert box["height"] >= 44


def create(page: Page, due: str) -> Locator:
    before = page.locator("[data-task]").count()
    form = page.locator("#create-task")
    form.locator('[name="kind"]').select_option("checklist")
    value = datetime.fromisoformat(due)
    form.locator('[name="due_date"]').fill(value.strftime("%d/%m/%Y"))
    form.locator('[name="due_time"]').fill(value.strftime("%H:%M"))
    submit(page, form.locator('button[value="create"]'))
    expect(page.locator("[data-tasks-state]")).to_have_attribute(
        "data-tasks-state", "success"
    )
    expect(page.locator("[data-task]")).to_have_count(before + 1)
    row = page.locator('[data-task-state="open"]').last
    expect(row).to_be_visible()
    identifier = row.get_attribute("data-task")
    assert identifier is not None
    return page.locator(f'[data-task="{identifier}"]')


@pytest.mark.parametrize("width", [1280, 375, 320])
def test_tasks_lifecycle_native_and_enhanced(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
    width: int,
) -> None:
    browser = renewal_page.context.browser
    assert browser is not None
    context = browser.new_context(
        viewport={"width": width, "height": 900},
        locale="pt-BR",
        timezone_id="Asia/Tokyo",
    )
    page = context.new_page()
    try:
        _sign_in_physician(page, renewal_base_url, availability_staff)
        url = f"{renewal_base_url}/clinics/{availability_staff['clinic_a']}/tasks/"
        page.goto(url)
        expect(
            page.get_by_role("heading", name=gettext("Tasks"), exact=True)
        ).to_be_visible()
        capture(page, renewal_base_url, renewal_artifact_root, "queue", width)
        row = create(
            page,
            {
                1280: "2035-01-01T09:00",
                375: "2035-02-01T09:00",
                320: "2035-03-01T09:00",
            }[width],
        )
        submit(page, row.locator('button[value="assign"]'))
        expect(row).to_have_attribute("data-task-state", "assigned")
        capture(page, renewal_base_url, renewal_artifact_root, "assigned", width)
        submit(page, row.locator('button[value="start"]'))
        expect(row).to_have_attribute("data-task-state", "in_progress")
        row.locator('[name="checked"]').check()
        submit(page, row.locator('button[value="complete"]'))
        expect(row).to_have_attribute("data-task-state", "done")
        capture(page, renewal_base_url, renewal_artifact_root, "completed", width)
        filters = page.locator(".task-filters").first
        filters.locator('[name="filter-state"]').select_option("open")
        submit(page, filters.locator('button[value="filter"]'))
        expect(page.locator("[data-tasks-empty]")).to_be_visible()
        page.keyboard.press("Tab")
        assert page.locator(":focus").count() == 1
        token = csrf(page)
        responses = [
            page.request.post(
                url,
                form={
                    "csrfmiddlewaretoken": token,
                    "action": "complete",
                    "task_id": str(uuid4()),
                    "expected_revision": "1",
                    "checked": "on",
                },
            )
            for _ in range(2)
        ]
        assert [response.status for response in responses] == [403, 403]
        assert responses[0].body() == responses[1].body()
        assert all("set-cookie" not in response.headers for response in responses)
        page.goto(f"{url}exceptions/")
        capture(page, renewal_base_url, renewal_artifact_root, "exceptions", width)
    finally:
        context.close()


def test_native_forms_without_javascript(
    renewal_page: Page, renewal_base_url: str, availability_staff: dict[str, str]
) -> None:
    browser = renewal_page.context.browser
    assert browser is not None
    context = browser.new_context(
        java_script_enabled=False, viewport={"width": 375, "height": 900}
    )
    page = context.new_page()
    try:
        _sign_in_physician(page, renewal_base_url, availability_staff)
        page.goto(f"{renewal_base_url}/clinics/{availability_staff['clinic_a']}/tasks/")
        row = create(page, "2035-06-01T09:00")
        submit(page, row.locator('button[value="assign"]'))
        submit(page, row.locator('button[value="start"]'))
        row.locator('[name="checked"]').check()
        submit(page, row.locator('button[value="complete"]'))
        expect(row).to_have_attribute("data-task-state", "done")
    finally:
        context.close()


def test_stale_command_and_network_failure_have_no_false_success(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
) -> None:
    browser = renewal_page.context.browser
    assert browser is not None
    context = browser.new_context(
        service_workers="block", viewport={"width": 375, "height": 900}
    )
    page = context.new_page()
    try:
        _sign_in_physician(page, renewal_base_url, availability_staff)
        url = f"{renewal_base_url}/clinics/{availability_staff['clinic_a']}/tasks/"
        page.goto(url)
        row = create(page, "2035-07-01T09:00")
        identifier = row.get_attribute("data-task")
        second = context.new_page()
        second.goto(url)
        remote = second.locator(f'[data-task="{identifier}"]')
        submit(second, remote.locator('button[value="assign"]'))
        submit(second, remote.locator('button[value="start"]'))
        with page.expect_response(
            lambda response: response.request.method == "POST"
        ) as stale:
            row.locator('button[value="assign"]').click()
        assert stale.value.status == 409
        expect(page.locator("#tasks-panel")).to_have_attribute(
            "data-tasks-state", "conflict"
        )
        expect(row).to_have_attribute("data-task-state", "in_progress")
        capture(page, renewal_base_url, renewal_artifact_root, "conflict", 375)

        def drop(route: Route) -> None:
            if route.request.method == "POST":
                route.abort()
            else:
                route.continue_()

        context.route(url, drop)
        row.locator('[name="checked"]').check()
        with page.expect_event(
            "requestfailed",
            predicate=lambda request: request.method == "POST" and request.url == url,
        ):
            row.locator('button[value="complete"]').click()
        expect(page.locator("#tasks-panel")).to_have_attribute(
            "data-tasks-state", "offline"
        )
        expect(row).to_have_attribute("data-task-state", "in_progress")
        capture(page, renewal_base_url, renewal_artifact_root, "offline", 375)
        context.unroute(url, drop)
        submit(page, row.locator('button[value="complete"]'))
        expect(row).to_have_attribute("data-task-state", "done")
        capture(page, renewal_base_url, renewal_artifact_root, "recovered", 375)
    finally:
        context.close()


def test_bulk_reassignment_requires_visible_preview(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    availability_staff: dict[str, str],
) -> None:
    staff = availability_staff
    manager = seed_manager(staff)
    page = renewal_page
    page.set_viewport_size({"width": 1280, "height": 900})
    sign_in_manager(page, renewal_base_url, staff, manager)
    page.goto(f"{renewal_base_url}/clinics/{staff['clinic_a']}/tasks/")
    first = create(page, "2035-04-01T09:00")
    second = create(page, "2035-05-01T09:00")
    first.locator('[name="task_ids"]').check()
    second.locator('[name="task_ids"]').check()
    page.locator("#bulk-owner").select_option(f"user:{staff['physician_a_id']}")
    submit(page, page.locator('button[value="bulk-preview"]'))
    expect(page.locator("[data-reassignment-preview]")).to_be_visible()
    expect(first).to_have_attribute("data-task-state", "open")
    expect(second).to_have_attribute("data-task-state", "open")
    capture(page, renewal_base_url, renewal_artifact_root, "preview", 1280)
    submit(page, page.locator('button[value="bulk-apply"]'))
    expect(first).to_have_attribute("data-task-state", "assigned")
    expect(second).to_have_attribute("data-task-state", "assigned")
    capture(page, renewal_base_url, renewal_artifact_root, "reassigned", 1280)
