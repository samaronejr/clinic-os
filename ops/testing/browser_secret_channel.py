"""Length-prefixed private-FD secret channel for the browser-server session.

Exactly seven frames cross this channel: four password-bearing frames and three
code-only enrollment confirms. Secrets live only in mutable buffers that are
zeroized immediately after the frame is written, and no frame content is ever
written to stdout, stderr, a log, or a file.
"""

from __future__ import annotations

import os
from typing import Final, Never

OWNER_START: Final = "owner-enroll-start"
OWNER_CONFIRM: Final = "owner-enroll-confirm"
ADMIN_START: Final = "admin-enroll-start"
ADMIN_CONFIRM: Final = "admin-enroll-confirm"
PHYSICIAN_START: Final = "physician-enroll-start"
PHYSICIAN_CONFIRM: Final = "physician-enroll-confirm"
RECEPTIONIST_LOGIN: Final = "receptionist-login"

FRAME_ORDER: Final = (
    OWNER_START,
    OWNER_CONFIRM,
    ADMIN_START,
    ADMIN_CONFIRM,
    PHYSICIAN_START,
    PHYSICIAN_CONFIRM,
    RECEPTIONIST_LOGIN,
)
PASSWORD_FRAMES: Final = frozenset(
    {OWNER_START, ADMIN_START, PHYSICIAN_START, RECEPTIONIST_LOGIN}
)
CODE_FRAMES: Final = frozenset({OWNER_CONFIRM, ADMIN_CONFIRM, PHYSICIAN_CONFIRM})
FRAME_COUNT: Final = len(FRAME_ORDER)
PASSWORD_FRAME_COUNT: Final = len(PASSWORD_FRAMES)
CODE_FRAME_COUNT: Final = len(CODE_FRAMES)
CODE_LENGTH: Final = 6
LENGTH_BYTES: Final = 4
MAX_FRAME_BYTES: Final = 4096
SEPARATOR: Final = b"\x00"
MIN_DESCRIPTOR: Final = 3


class SecretChannelError(RuntimeError):
    """Reject a malformed, misordered, or secret-leaking frame."""

    def __init__(self, reason: str) -> None:
        """Expose one precise non-identifying channel failure."""
        super().__init__(f"secret channel rejected: {reason}")


def _fail(reason: str) -> Never:
    raise SecretChannelError(reason)


def _zeroize(buffer: bytearray) -> None:
    for index in range(len(buffer)):
        buffer[index] = 0


def encode_frame(kind: str, sequence: int, secret: bytearray) -> bytearray:
    """Return one length-prefixed frame body built from a mutable buffer."""
    body = bytearray()
    body += kind.encode("ascii")
    body += SEPARATOR
    body += str(sequence).encode("ascii")
    body += SEPARATOR
    body += secret
    if len(body) > MAX_FRAME_BYTES:
        _zeroize(body)
        _fail("frame exceeds the bounded size")
    framed = bytearray(len(body).to_bytes(LENGTH_BYTES, "big"))
    framed += body
    _zeroize(body)
    return framed


def decode_frame(raw: bytes) -> tuple[str, int, bytes]:
    """Decode one bounded length-prefixed frame into its three fields."""
    if len(raw) < LENGTH_BYTES:
        _fail("frame is shorter than its length prefix")
    declared = int.from_bytes(raw[:LENGTH_BYTES], "big")
    body = raw[LENGTH_BYTES:]
    if declared != len(body) or declared > MAX_FRAME_BYTES:
        _fail("frame length prefix does not match its body")
    parts = body.split(SEPARATOR, 2)
    expected_parts = 3
    if len(parts) != expected_parts:
        _fail("frame body is not exactly three fields")
    kind = parts[0].decode("ascii")
    if kind not in FRAME_ORDER or not parts[1].isdigit():
        _fail("frame kind or sequence is invalid")
    return kind, int(parts[1]), bytes(parts[2])


class SecretChannel:
    """Write exactly the seven ordered secret frames to one private FD."""

    def __init__(self, descriptor: int) -> None:
        """Bind the channel to one already-open private output descriptor."""
        if descriptor < MIN_DESCRIPTOR:
            _fail("secret channel requires a private descriptor")
        self._descriptor = descriptor
        self._sent: list[str] = []
        self._closed = False

    @property
    def sent(self) -> tuple[str, ...]:
        """Return the frame kinds already written, in order."""
        return tuple(self._sent)

    def send(
        self,
        kind: str,
        *,
        password: bytearray | None = None,
        code: bytearray | None = None,
    ) -> None:
        """Write one ordered frame carrying a password or a code, never both."""
        secret = self._validated_secret(kind, password, code)
        framed = encode_frame(kind, len(self._sent), secret)
        try:
            written = os.write(self._descriptor, framed)
        finally:
            _zeroize(framed)
            _zeroize(secret)
        if written != len(framed):
            _fail("frame was not written atomically")
        self._sent.append(kind)

    def _validated_secret(
        self,
        kind: str,
        password: bytearray | None,
        code: bytearray | None,
    ) -> bytearray:
        if self._closed:
            _fail("channel is already closed")
        if password is not None and code is not None:
            _fail("a combined password and code frame is forbidden")
        if password is None and code is None:
            _fail("frame carries no secret")
        if len(self._sent) >= FRAME_COUNT:
            _fail("channel already sent every permitted frame")
        expected = FRAME_ORDER[len(self._sent)]
        if kind != expected:
            _fail(f"expected {expected} but received {kind}")
        if password is not None and kind not in PASSWORD_FRAMES:
            _fail(f"{kind} is not a password-bearing frame")
        if code is not None and kind not in CODE_FRAMES:
            _fail(f"{kind} is not a code-only frame")
        secret = password if password is not None else code
        if secret is None or not secret:
            _fail("secret buffer is empty")
        if code is not None and len(code) != CODE_LENGTH:
            _fail("enrollment code must be exactly six bytes")
        return secret

    def close(self) -> None:
        """Require the complete frame set, then close the private descriptor."""
        if self._closed:
            return
        os.close(self._descriptor)
        self._closed = True
        if tuple(self._sent) != FRAME_ORDER:
            _fail("channel closed before sending every ordered frame")
