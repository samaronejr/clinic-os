"""Runner-resident authenticated contexts and the supervisor session record."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, Never

import rfc8785

from ops.testing.browser_runner_contract import selected_suites

if TYPE_CHECKING:
    from pathlib import Path

    from ops.testing.isolation_common import JsonObject, JsonValue

TEST_ORIGIN: Final = "http://phase1a-browser.qa.clinic-os.test"
EXTRA_HOST: Final = "phase1a-browser.qa.clinic-os.test"
HSTS_SUFFIX: Final = ".dev"
DISPATCH_KEYS: Final = frozenset(
    {"argv_sha256", "claim_id", "schema_version", "sequence", "suite_id"}
)
PERSONAS: Final = ("clinic-admin", "owner", "physician", "receptionist")


class SupervisorSessionError(RuntimeError):
    """Reject an unauthenticated, serialized, or misbound session action."""

    def __init__(self, reason: str) -> None:
        """Expose one precise non-identifying session failure."""
        super().__init__(f"browser supervisor session rejected: {reason}")


def _fail(reason: str) -> Never:
    raise SupervisorSessionError(reason)


@dataclass(frozen=True, slots=True)
class BrowserSupervisorSession:
    """Bind one claimed runner session to its staging and publisher identity."""

    claim_id: str
    staging_root: Path
    publisher_root: Path
    publisher_authorization_id: str
    origin: str = TEST_ORIGIN

    def __post_init__(self) -> None:
        """Require absolute claim-owned roots on the non-HSTS test origin."""
        if not self.staging_root.is_absolute() or self.staging_root.is_symlink():
            _fail("staging root must be an absolute non-symlink path")
        if not self.publisher_root.is_absolute():
            _fail("publisher root must be absolute")
        if self.claim_id not in self.staging_root.as_posix():
            _fail("staging root is not bound to the session claim")
        if not self.origin.startswith("http://") or HSTS_SUFFIX in self.origin:
            _fail("session must use the non-HSTS test origin")
        if EXTRA_HOST not in self.origin:
            _fail("session origin is not the reserved browser host alias")


class ClinicBrowserContexts:
    """Retain authenticated browser contexts inside the runner process only."""

    def __init__(self) -> None:
        """Start with no retained persona context."""
        self._contexts: dict[str, object] = {}

    @property
    def personas(self) -> tuple[str, ...]:
        """Return the personas whose contexts are currently retained."""
        return tuple(sorted(self._contexts))

    def retain(self, persona: str, context: object) -> None:
        """Retain one live authenticated context for a known persona."""
        if persona not in PERSONAS:
            _fail(f"{persona} is not a known browser persona")
        if persona in self._contexts:
            _fail(f"{persona} context is already retained")
        self._contexts[persona] = context

    def borrow(self, persona: str) -> object:
        """Return the retained live context without copying or exporting it."""
        if persona not in self._contexts:
            _fail(f"{persona} context is not retained")
        return self._contexts[persona]

    def discard(self) -> None:
        """Drop every retained context at teardown."""
        self._contexts.clear()

    def __reduce__(self) -> Never:
        """Refuse pickling so an authenticated context can never be exported."""
        _fail("authenticated browser contexts are never serialized")

    def __getstate__(self) -> Never:
        """Refuse state export so no cookie or storage state can escape."""
        _fail("authenticated browser contexts are never serialized")


def dispatch_frame(
    claim_id: str,
    sequence: int,
    suite_id: str,
    argv: list[str],
) -> bytes:
    """Build one claim, sequence, suite, and argv-hash authenticated frame."""
    selected_suites([suite_id], [suite_id])
    if sequence < 0:
        _fail("dispatch sequence must be nonnegative")
    entries: list[JsonValue] = list(argv)
    value: JsonObject = {
        "argv_sha256": hashlib.sha256(rfc8785.dumps(entries)).hexdigest(),
        "claim_id": claim_id,
        "schema_version": 1,
        "sequence": sequence,
        "suite_id": suite_id,
    }
    return rfc8785.dumps(value) + b"\n"


def authenticate_dispatch(
    raw: bytes,
    claim_id: str,
    sequence: int,
    suite_id: str,
    argv: list[str],
) -> None:
    """Reject any dispatch frame not bound to this claim, order, and argv."""
    expected = dispatch_frame(claim_id, sequence, suite_id, argv)
    if raw != expected:
        _fail("dispatch frame is not authenticated for this session")


@dataclass(slots=True)
class RunnerSuiteRegistry:
    """Expose only allowlisted zero-argument in-process suite entrypoints."""

    entries: dict[str, object] = field(default_factory=dict)

    def register(self, suite_id: str, entrypoint: object) -> None:
        """Register one allowlisted callable for an available suite."""
        selected_suites([suite_id], [suite_id])
        if not callable(entrypoint):
            _fail("suite entrypoint is not callable")
        module = getattr(entrypoint, "__module__", "")
        if not module.startswith("ops.testing.browser_suites."):
            _fail("suite entrypoint is not an allowlisted runner suite")
        if suite_id in self.entries:
            _fail(f"{suite_id} is already registered")
        self.entries[suite_id] = entrypoint

    def resolve(self, suite_id: str) -> object:
        """Return the registered entrypoint for one available suite."""
        if suite_id not in self.entries:
            _fail(f"{suite_id} is not a registered runner suite")
        return self.entries[suite_id]
