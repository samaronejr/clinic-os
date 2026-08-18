"""Capture the fixed production-HTTPS smoke with retained runner contexts."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Final, Never, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Callable

    from ops.testing.browser_supervisor_session import ClinicBrowserContexts

SUITE_ID: Final = "runtime-https"
ARTIFACT_PREFIX: Final = "browser/runtime-https"
PRODUCTION_ORIGIN: Final = "https://phase1a.qa.clinic-os.dev:8443"
PERSONAS: Final = ("clinic-admin", "owner", "physician", "receptionist")
NAVIGATION_TIMEOUT_MS: Final = 20_000


class RuntimeHttpsSuiteError(RuntimeError):
    """Reject a substituted origin or non-live retained context."""


def _fail(reason: str) -> Never:
    raise RuntimeHttpsSuiteError(reason)


@runtime_checkable
class _Page(Protocol):
    def goto(self, url: str, *, wait_until: str, timeout: int) -> object: ...

    def screenshot(self, *, full_page: bool) -> bytes: ...

    def close(self) -> None: ...


@runtime_checkable
class _Context(Protocol):
    def new_page(self) -> _Page: ...


def build_runtime_https_suite(
    contexts: ClinicBrowserContexts,
    origin: str = PRODUCTION_ORIGIN,
) -> Callable[[], dict[str, bytes]]:
    """Bind one zero-argument suite to the existing nonserializable contexts."""
    if origin != PRODUCTION_ORIGIN or contexts.personas != PERSONAS:
        _fail("runtime HTTPS suite binding rejected")

    def run() -> dict[str, bytes]:
        artifacts: dict[str, bytes] = {}
        for persona in PERSONAS:
            retained = contexts.borrow(persona)
            if not isinstance(retained, _Context):
                _fail("retained browser context rejected")
            page = retained.new_page()
            if not isinstance(page, _Page):
                _fail("retained browser page rejected")
            try:
                page.goto(
                    f"{origin}/readyz",
                    wait_until="networkidle",
                    timeout=NAVIGATION_TIMEOUT_MS,
                )
                capture = page.screenshot(full_page=True)
            finally:
                page.close()
            if not capture.startswith(b"\x89PNG\r\n\x1a\n"):
                _fail("runtime HTTPS capture rejected")
            artifacts[f"{ARTIFACT_PREFIX}/{persona}.png"] = capture
        summary = {
            "origin": origin,
            "personas": list(PERSONAS),
            "schema_version": 1,
        }
        artifacts[f"{ARTIFACT_PREFIX}/summary.json"] = (
            json.dumps(summary, separators=(",", ":"), sort_keys=True).encode("utf-8")
            + b"\n"
        )
        return dict(sorted(artifacts.items()))

    return run
