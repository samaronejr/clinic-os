"""Exact request and HTMX-settle subscriptions shared by patient-record journeys."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from playwright.sync_api import expect

if TYPE_CHECKING:
    from playwright.sync_api import Locator, Page


def submit(page: Page, button: Locator, *, status: int = 200) -> None:
    """Subscribe before the native command and await its actual rendered response."""
    before = page.locator("#patient-workspace").get_attribute("data-workspace-render")
    token = uuid4().hex
    enhanced = page.evaluate("() => Boolean(window.htmx)")
    if enhanced:
        page.evaluate(
            """token => {
              const settled = event => {
                if (event.detail.target?.id !== "patient-workspace") return;
                document.documentElement.dataset.workspaceSettled = token;
                document.removeEventListener("htmx:afterSettle", settled);
              };
              document.addEventListener("htmx:afterSettle", settled);
            }""",
            token,
        )
    url = page.url
    with page.expect_response(
        lambda response: response.url == url and response.request.method == "POST"
    ) as pending:
        button.click()
    assert pending.value.status == status
    expect(page.locator("#patient-workspace")).not_to_have_attribute(
        "data-workspace-render", before or ""
    )
    if enhanced:
        expect(page.locator("html")).to_have_attribute("data-workspace-settled", token)
    expect(button).to_be_enabled()
    expect(page.locator("#patient-workspace.htmx-settling")).to_have_count(0)


def tab(page: Page, key: str) -> None:
    """Navigate through the same native form command as a clinician."""
    submit(page, page.locator(f'button[name="tab"][value="{key}"]'))
