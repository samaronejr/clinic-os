"""Fixtures bound to the renewal runner's private environment contract.

The runner exports ``CLINIC_RENEWAL_BASE_URL`` and
``CLINIC_RENEWAL_ARTIFACT_ROOT`` plus the browser executable and the seeded
owner credentials. Outside the runner these fixtures skip, and the runner's
junit gate rejects any skipped suite, so a bare pytest run can never fake a
green browser pass.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from playwright.sync_api import sync_playwright

if TYPE_CHECKING:
    from collections.abc import Iterator

    from playwright.sync_api import Page

NAVIGATION_TIMEOUT_MS = 20_000


def _required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        pytest.skip(f"{name} is only exported by the renewal runner")
    return value


@pytest.fixture(scope="session")
def renewal_base_url() -> str:
    """Return the supervised loopback base URL for this run."""
    return _required("CLINIC_RENEWAL_BASE_URL").rstrip("/")


@pytest.fixture(scope="session")
def renewal_artifact_root() -> Path:
    """Return the runner-owned artifact directory for browser captures."""
    root = Path(_required("CLINIC_RENEWAL_ARTIFACT_ROOT"))
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root


@pytest.fixture(scope="session")
def renewal_owner() -> dict[str, str]:
    """Return the seeded owner credentials for the login journey."""
    return {
        "username": _required("CLINIC_RENEWAL_USERNAME"),
        "password": _required("CLINIC_RENEWAL_PASSWORD"),
    }


@pytest.fixture(scope="session")
def renewal_page() -> Iterator[Page]:
    """Launch the runner-selected real Chromium and yield one page."""
    executable = _required("CLINIC_RENEWAL_BROWSER_EXECUTABLE")
    with sync_playwright() as driver:
        browser = driver.chromium.launch(
            executable_path=executable,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context()
        page = context.new_page()
        page.set_default_timeout(NAVIGATION_TIMEOUT_MS)
        yield page
        context.close()
        browser.close()


@pytest.fixture(scope="session")
def browser_report(
    renewal_artifact_root: Path,
) -> Iterator[dict[str, object]]:
    """Collect per-test observations; a finalizer writes the report file."""
    report: dict[str, object] = {"schema_version": 1, "checks": []}
    yield report
    destination = renewal_artifact_root / "smoke-report.json"
    destination.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n")
    destination.chmod(0o600)
