"""The closed set of external effects one browser-server session may cause.

Keeping the protocol separate from the ordering flow means the real ledger,
Docker, Gunicorn, and pseudo-terminal implementation and the recording test
double are both checked against exactly the same declared surface.
"""

from __future__ import annotations

from typing import Protocol


class BrowserSessionEffects(Protocol):
    """Every external effect the browser-server session is allowed to cause."""

    def refresh_ledger(self) -> None:
        """Refresh Todo 1's active ledger before any external effect."""
        ...

    def activate_materializer(self) -> None:
        """Activate the materializer claim the TLS stack depends on."""
        ...

    def activate_database(self) -> None:
        """Reserve, start, and activate the TLS PostgreSQL stack."""
        ...

    def run_owner_release(self) -> None:
        """Run the owner release and migrations against the active stack."""
        ...

    def bootstrap_owner(self) -> None:
        """Execute only Todo 13's TOTP-exempt clinic bootstrap."""
        ...

    def export_ca(self) -> None:
        """Activate the executor-owned public CA export filesystem claim."""
        ...

    def reserve_process_claim(self) -> int:
        """Reserve the browser-server process claim and return its port."""
        ...

    def request_start_master(self, port: int) -> int:
        """Ask the supervisor to fork and exec the master; return its pid."""
        ...

    def observe_workers(self) -> int:
        """Return the number of direct workers observed for the master."""
        ...

    def probe_candidate(self) -> list[str]:
        """Probe the clean candidate and return its available suite ids."""
        ...

    def runner_intent(self) -> str:
        """Durably fsync runner create intent and return its digest."""
        ...

    def runner_create(self) -> None:
        """Create or adopt the deterministic runner container."""
        ...

    def runner_inspect(self) -> str:
        """Return the inspected Docker state of the created runner."""
        ...

    def runner_prepare(self) -> None:
        """Record the exact created-state identity as prepared."""
        ...

    def runner_start(self) -> float:
        """Start the runner and return the elapsed seconds to attach."""
        ...

    def runner_attest(self) -> str:
        """Return the runner's fixed bootstrap attestation payload."""
        ...

    def runner_activate(self) -> None:
        """Atomically activate and acknowledge the runner claim."""
        ...

    def send_password_frame(self, kind: str, persona: str) -> None:
        """Send one password-bearing frame from a mutable buffer."""
        ...

    def send_code_frame(self, kind: str, code: bytearray) -> None:
        """Send one code-only enrollment confirm frame."""
        ...

    def await_pending_ready(self, persona: str) -> None:
        """Block until the runner acknowledges pending-ready."""
        ...

    def run_helper(self, persona: str, mode: str) -> tuple[int, bytearray]:
        """Run one private-FD helper and return its counter and code."""
        ...

    def provision_staff(self, persona: str, code: bytearray) -> None:
        """Drive one Todo 13 provision_staff pseudo-terminal."""
        ...

    def authorize_suite(self, suite_id: str) -> bytes:
        """Return the claim, sequence, suite, and argv authenticated frame."""
        ...

    def verify_authorization(self, suite_id: str, frame: bytes) -> bool:
        """Report whether the runner accepted that frame for this session."""
        ...

    def dispatch_suite(self, suite_id: str) -> dict[str, bytes]:
        """Dispatch the suite in-process and return bounded artifacts."""
        ...

    def publish_frames(self, frames: tuple[bytes, ...]) -> tuple[str, ...]:
        """Export the bounded frames to the host and return published paths."""
        ...

    def acknowledge_publication(self, suite_id: str, digest: str) -> bytes:
        """Return the host's final acknowledgement of one published manifest."""
        ...

    def runner_remove(self) -> None:
        """Record remove intent, remove, and prove the runner absent."""
        ...

    def stop_master(self) -> None:
        """Terminate the supervised master group and reap it."""
        ...

    def release_database(self) -> None:
        """Tear down and release the TLS PostgreSQL stack claim."""
        ...
