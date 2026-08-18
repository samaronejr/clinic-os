from __future__ import annotations

import os
from typing import TYPE_CHECKING, Final

from ops.testing.browser_secret_channel import (
    LENGTH_BYTES,
    SecretChannel,
    decode_frame,
)
from ops.testing.browser_totp_helpers import (
    CONFIRMED,
    HelperOutcome,
    HelperSequence,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

SYNTHETIC_CODE: Final = b"123456"
SYNTHETIC_PASSWORDS: Final = {
    "owner": b"Sy7-Synthetic-Owner-Buffer",
    "clinic-admin": b"Sy7-Synthetic-Admin-Buffer",
    "physician": b"Sy7-Synthetic-Physician-Buffer",
    "receptionist": b"Sy7-Synthetic-Reception-Buffer",
}
CLEAN_OUTCOME: Final = HelperOutcome(
    code_length=6,
    stdout_bytes=0,
    stderr_bytes=0,
    open_descriptors=0,
)


def read_frames(descriptor: int) -> tuple[tuple[str, int, bytes], ...]:
    """Drain a private pipe and decode every complete length-prefixed frame."""
    chunks: list[bytes] = []
    while True:
        chunk = os.read(descriptor, 65536)
        if not chunk:
            break
        chunks.append(chunk)
    raw = b"".join(chunks)
    frames: list[tuple[str, int, bytes]] = []
    offset = 0
    while offset < len(raw):
        declared = int.from_bytes(raw[offset : offset + LENGTH_BYTES], "big")
        end = offset + LENGTH_BYTES + declared
        frames.append(decode_frame(raw[offset:end]))
        offset = end
    return tuple(frames)


class RecordingEffects:
    """Record every browser-session effect while exercising real invariants."""

    def __init__(
        self,
        channel: SecretChannel,
        helpers: HelperSequence,
        faults: frozenset[str] = frozenset(),
    ) -> None:
        self.channel = channel
        self.helpers = helpers
        self.faults = faults
        self.calls: list[str] = []
        self.provisioned: list[str] = []
        self.pending_ready: list[str] = []
        self._counter = 100

    def _note(self, name: str) -> None:
        self.calls.append(name)

    def refresh_ledger(self) -> None:
        self._note("refresh_ledger")

    def activate_materializer(self) -> None:
        self._note("activate_materializer")

    def activate_database(self) -> None:
        self._note("activate_database")

    def run_owner_release(self) -> None:
        self._note("run_owner_release")

    def bootstrap_owner(self) -> None:
        self._note("bootstrap_owner")

    def export_ca(self) -> None:
        self._note("export_ca")

    def reserve_process_claim(self) -> int:
        self._note("reserve_process_claim")
        return 58419

    def request_start_master(self, port: int) -> int:
        self._note(f"request_start_master:{port}")
        return 4242

    def observe_workers(self) -> int:
        self._note("observe_workers")
        return 1 if "workers" in self.faults else 2

    def probe_candidate(self) -> list[str]:
        self._note("probe_candidate")
        if "suite-ids" in self.faults:
            return ["availability", "patient"]
        return ["patient"]

    def runner_intent(self) -> str:
        self._note("runner_intent")
        return "" if "no-intent" in self.faults else "a" * 64

    def runner_create(self) -> None:
        self._note("runner_create")

    def runner_inspect(self) -> str:
        self._note("runner_inspect")
        return "running" if "runner-state" in self.faults else "created"

    def runner_prepare(self) -> None:
        self._note("runner_prepare")

    def runner_start(self) -> float:
        self._note("runner_start")
        return 9.5 if "runner-slow" in self.faults else 0.4

    def runner_attest(self) -> str:
        self._note("runner_attest")
        return "" if "no-attest" in self.faults else "attested"

    def runner_activate(self) -> None:
        self._note("runner_activate")

    def send_password_frame(self, kind: str, persona: str) -> None:
        self._note(f"password:{kind}")
        self.channel.send(kind, password=bytearray(SYNTHETIC_PASSWORDS[persona]))

    def send_code_frame(self, kind: str, code: bytearray) -> None:
        self._note(f"code:{kind}")
        self.channel.send(kind, code=code)

    def await_pending_ready(self, persona: str) -> None:
        self._note(f"pending_ready:{persona}")
        self.pending_ready.append(persona)

    def run_helper(self, persona: str, mode: str) -> tuple[int, bytearray]:
        self._note(f"helper:{persona}:{mode}")
        if mode == CONFIRMED:
            self._counter += 1
        self.helpers.record(persona, mode, self._counter, CLEAN_OUTCOME)
        return self._counter, bytearray(SYNTHETIC_CODE)

    def provision_staff(self, persona: str, code: bytearray) -> None:
        self._note(f"provision:{persona}")
        if len(code) != len(SYNTHETIC_CODE):
            message = "provision received a malformed code buffer"
            raise AssertionError(message)
        self.provisioned.append(persona)

    def dispatch_suite(self, suite_id: str) -> dict[str, bytes]:
        self._note(f"dispatch:{suite_id}")
        if "no-artifacts" in self.faults:
            return {}
        return {f"browser/{suite_id}/summary.json": b"{}\n"}

    def runner_remove(self) -> None:
        self._note("runner_remove")

    def stop_master(self) -> None:
        self._note("stop_master")

    def release_database(self) -> None:
        self._note("release_database")


def session_channel() -> Iterator[tuple[SecretChannel, int]]:
    """Yield a real secret channel bound to a private pipe and its read end."""
    read_fd, write_fd = os.pipe()
    yield SecretChannel(write_fd), read_fd
