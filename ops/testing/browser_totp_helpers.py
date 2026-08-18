"""Sequence guard for the exactly six private-FD TOTP helper processes.

One owner pending-enrollment helper supplies the owner's own confirm code. Three
owner confirmed-next-counter helpers, each strictly after the previously
committed counter, feed the three `provision_staff` pseudo-terminals. The
clinic-admin and physician each then use one pending-enrollment helper. The
receptionist never has a device and therefore never has a helper.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final, Never

OWNER: Final = "owner"
CLINIC_ADMIN: Final = "clinic-admin"
PHYSICIAN: Final = "physician"
RECEPTIONIST: Final = "receptionist"
PENDING: Final = "pending-enrollment"
CONFIRMED: Final = "confirmed-next-counter"

HELPER_SEQUENCE: Final = (
    (OWNER, PENDING),
    (OWNER, CONFIRMED),
    (OWNER, CONFIRMED),
    (OWNER, CONFIRMED),
    (CLINIC_ADMIN, PENDING),
    (PHYSICIAN, PENDING),
)
HELPER_COUNT: Final = len(HELPER_SEQUENCE)
DEVICELESS_PERSONAS: Final = frozenset({RECEPTIONIST})
CODE_LENGTH: Final = 6


class HelperSequenceError(RuntimeError):
    """Reject a helper invoked out of order, reused, or leaking bytes."""

    def __init__(self, reason: str) -> None:
        """Expose one precise non-identifying helper-sequence failure."""
        super().__init__(f"totp helper sequence rejected: {reason}")


def _fail(reason: str) -> Never:
    raise HelperSequenceError(reason)


@dataclass(frozen=True, slots=True)
class HelperOutcome:
    """Observable byte and descriptor residue of one helper process."""

    code_length: int
    stdout_bytes: int
    stderr_bytes: int
    open_descriptors: int


@dataclass(frozen=True, slots=True)
class HelperInvocation:
    """One completed helper process and the counter its code consumed."""

    persona: str
    mode: str
    counter: int


@dataclass(slots=True)
class HelperSequence:
    """Track the closed six-process helper sequence and its counters."""

    invocations: list[HelperInvocation] = field(default_factory=list)
    _last_counter: int = -1

    @property
    def completed(self) -> int:
        """Return how many helper processes have already run."""
        return len(self.invocations)

    def expected_next(self) -> tuple[str, str]:
        """Return the persona and mode the next helper process must use."""
        if self.completed >= HELPER_COUNT:
            _fail("every permitted helper process has already run")
        return HELPER_SEQUENCE[self.completed]

    def record(
        self,
        persona: str,
        mode: str,
        counter: int,
        outcome: HelperOutcome,
    ) -> HelperInvocation:
        """Validate and record one completed helper process invocation."""
        if persona in DEVICELESS_PERSONAS:
            _fail(f"{persona} must never own a TOTP device")
        expected_persona, expected_mode = self.expected_next()
        if (persona, mode) != (expected_persona, expected_mode):
            _fail(
                f"expected {expected_persona}/{expected_mode} "
                f"but observed {persona}/{mode}"
            )
        if outcome.code_length != CODE_LENGTH:
            _fail("helper emitted a code that is not exactly six bytes")
        if outcome.stdout_bytes or outcome.stderr_bytes:
            _fail("helper emitted diagnostic bytes")
        if outcome.open_descriptors:
            _fail("helper leaked an inherited descriptor")
        if mode == CONFIRMED and counter <= self._last_counter:
            _fail("confirmed-next-counter helper reused a counter")
        if mode == CONFIRMED:
            self._last_counter = counter
        invocation = HelperInvocation(persona=persona, mode=mode, counter=counter)
        self.invocations.append(invocation)
        return invocation

    def require_complete(self) -> None:
        """Require exactly the six ordered helper processes and no residue."""
        if self.completed != HELPER_COUNT:
            _fail(
                f"expected {HELPER_COUNT} helper processes "
                f"but observed {self.completed}"
            )
        observed = tuple((item.persona, item.mode) for item in self.invocations)
        if observed != HELPER_SEQUENCE:
            _fail("helper processes did not follow the closed sequence")
        counters = [item.counter for item in self.invocations if item.mode == CONFIRMED]
        if counters != sorted(set(counters)):
            _fail("owner confirmed counters are not strictly increasing")
