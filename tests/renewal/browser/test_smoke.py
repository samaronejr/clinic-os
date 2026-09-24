"""Smoke suite: open the real login and readiness surfaces as clinic_app.

``/readyz`` only reports ``ok`` when the serving connection resolves to
``current_user = clinic_app`` on the ``clinic_app`` schema with every leaf
migration applied, so a 200 is the runtime-role assertion.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import Page

OK_STATUS = 200
FOUND_STATUS = 302


def _capture(page: Page, artifact_root: Path, label: str) -> str:
    destination = artifact_root / f"{label}.png"
    destination.write_bytes(page.screenshot(full_page=True))
    destination.chmod(0o600)
    return destination.name


def test_readiness_reports_the_migrated_app_role(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    browser_report: dict[str, object],
) -> None:
    response = renewal_page.goto(f"{renewal_base_url}/readyz", wait_until="load")
    assert response is not None
    assert response.status == OK_STATUS
    body = json.loads(response.body())
    assert body == {"status": "ok"}
    checks = browser_report["checks"]
    assert isinstance(checks, list)
    checks.append(
        {
            "assertion": "readyz ok implies current_user=clinic_app",
            "capture": _capture(renewal_page, renewal_artifact_root, "readyz"),
            "surface": "readyz",
        }
    )


def test_login_surface_renders_the_real_csrf_form(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    browser_report: dict[str, object],
) -> None:
    response = renewal_page.goto(f"{renewal_base_url}/auth/login/", wait_until="load")
    assert response is not None
    assert response.status == OK_STATUS
    renewal_page.wait_for_selector("input[name=csrfmiddlewaretoken]", state="attached")
    renewal_page.wait_for_selector("#id_username")
    renewal_page.wait_for_selector("#id_password")
    checks = browser_report["checks"]
    assert isinstance(checks, list)
    checks.append(
        {
            "assertion": "login form renders with CSRF token",
            "capture": _capture(renewal_page, renewal_artifact_root, "login"),
            "surface": "auth/login",
        }
    )


def test_owner_login_establishes_a_session_and_enters_the_totp_flow(
    renewal_page: Page,
    renewal_base_url: str,
    renewal_artifact_root: Path,
    renewal_owner: dict[str, str],
    browser_report: dict[str, object],
) -> None:
    renewal_page.goto(f"{renewal_base_url}/auth/login/", wait_until="load")
    renewal_page.fill("#id_username", renewal_owner["username"])
    renewal_page.fill("#id_password", renewal_owner["password"])
    with renewal_page.expect_navigation(wait_until="load"):
        renewal_page.click("button[type=submit]")
    assert any(
        cookie["name"] == "sessionid" for cookie in renewal_page.context.cookies()
    )
    # The owner is a privileged role: the protected default target must hand
    # the session to the TOTP enrollment flow, never render it directly.
    renewal_page.wait_for_url("**/auth/enroll/**")
    checks = browser_report["checks"]
    assert isinstance(checks, list)
    checks.append(
        {
            "assertion": "owner login yields a session and the TOTP flow",
            "capture": _capture(renewal_page, renewal_artifact_root, "login-enrolled"),
            "surface": "auth/enroll",
        }
    )
