from __future__ import annotations

import json
from dataclasses import dataclass, field

from ops.testing.browser_suites.runtime_https import build_runtime_https_suite
from ops.testing.browser_supervisor_session import ClinicBrowserContexts

ORIGIN = "https://phase1a.qa.clinic-os.dev:8443"
PNG = b"\x89PNG\r\n\x1a\nsynthetic-runtime-capture"


@dataclass
class FakePage:
    persona: str
    navigated: list[str] = field(default_factory=list)
    closed: bool = False

    def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
        assert wait_until == "networkidle"
        assert timeout == 20_000
        self.navigated.append(url)

    def screenshot(self, *, full_page: bool) -> bytes:
        assert full_page is True
        return PNG + self.persona.encode("ascii")

    def close(self) -> None:
        self.closed = True


@dataclass
class FakeContext:
    persona: str
    pages: list[FakePage] = field(default_factory=list)

    def new_page(self) -> FakePage:
        page = FakePage(self.persona)
        self.pages.append(page)
        return page


def test_runtime_https_reuses_retained_contexts_and_exports_fixed_artifacts() -> None:
    contexts = ClinicBrowserContexts()
    retained = {
        persona: FakeContext(persona)
        for persona in ("clinic-admin", "owner", "physician", "receptionist")
    }
    for persona, context in retained.items():
        contexts.retain(persona, context)

    artifacts = build_runtime_https_suite(contexts, ORIGIN)()

    assert list(artifacts) == [
        "browser/runtime-https/clinic-admin.png",
        "browser/runtime-https/owner.png",
        "browser/runtime-https/physician.png",
        "browser/runtime-https/receptionist.png",
        "browser/runtime-https/summary.json",
    ]
    summary = json.loads(artifacts["browser/runtime-https/summary.json"])
    assert summary == {
        "origin": ORIGIN,
        "personas": ["clinic-admin", "owner", "physician", "receptionist"],
        "schema_version": 1,
    }
    for persona, context in retained.items():
        assert context is contexts.borrow(persona)
        assert context.pages[0].navigated == [f"{ORIGIN}/readyz"]
        assert context.pages[0].closed is True
