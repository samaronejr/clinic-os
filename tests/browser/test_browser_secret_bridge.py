from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from ops.testing.browser_secret_channel import (
    ADMIN_START,
    CODE_FRAME_COUNT,
    FRAME_COUNT,
    FRAME_ORDER,
    OWNER_CONFIRM,
    OWNER_START,
    PASSWORD_FRAME_COUNT,
    SecretChannel,
    SecretChannelError,
    decode_frame,
    encode_frame,
)
from ops.testing.browser_server_flow import run_session
from ops.testing.browser_server_journal import BarrierJournal
from ops.testing.browser_totp_helpers import (
    CONFIRMED,
    HELPER_COUNT,
    HELPER_SEQUENCE,
    OWNER,
    PENDING,
    PHYSICIAN,
    HelperOutcome,
    HelperSequence,
    HelperSequenceError,
)

from browser.browser_server_fakes import (
    CLEAN_OUTCOME,
    SYNTHETIC_CODE,
    RecordingEffects,
    read_frames,
)

if TYPE_CHECKING:
    from pathlib import Path


def _run(tmp_path: Path) -> tuple[BarrierJournal, RecordingEffects, int]:
    read_fd, write_fd = os.pipe()
    channel = SecretChannel(write_fd)
    helpers = HelperSequence()
    effects = RecordingEffects(channel, helpers)
    journal = BarrierJournal(tmp_path / "bridge.jsonl")
    run_session(effects, journal, helpers)
    channel.close()
    return journal, effects, read_fd


def test_session_sends_exactly_seven_ordered_secret_frames(tmp_path: Path) -> None:
    _, _, read_fd = _run(tmp_path)

    frames = read_frames(read_fd)
    assert len(frames) == FRAME_COUNT
    assert tuple(kind for kind, _, _ in frames) == FRAME_ORDER
    assert [sequence for _, sequence, _ in frames] == list(range(FRAME_COUNT))
    codes = [body for kind, _, body in frames if kind.endswith("-confirm")]
    passwords = [body for kind, _, body in frames if not kind.endswith("-confirm")]
    assert len(passwords) == PASSWORD_FRAME_COUNT
    assert len(codes) == CODE_FRAME_COUNT
    assert all(body == SYNTHETIC_CODE for body in codes)


def test_session_runs_exactly_six_ordered_helper_processes(tmp_path: Path) -> None:
    read_fd, write_fd = os.pipe()
    helpers = HelperSequence()
    effects = RecordingEffects(SecretChannel(write_fd), helpers)
    run_session(effects, BarrierJournal(tmp_path / "s.jsonl"), helpers)

    assert helpers.completed == HELPER_COUNT
    assert tuple((i.persona, i.mode) for i in helpers.invocations) == HELPER_SEQUENCE
    counters = [i.counter for i in helpers.invocations if i.mode == CONFIRMED]
    assert counters == sorted(set(counters))
    helpers.require_complete()
    os.close(read_fd)
    os.close(write_fd)


def test_secret_channel_rejects_combined_and_misordered_frames() -> None:
    read_fd, write_fd = os.pipe()
    channel = SecretChannel(write_fd)
    with pytest.raises(SecretChannelError, match="combined password and code"):
        channel.send(OWNER_START, password=bytearray(b"p"), code=bytearray(b"123456"))
    with pytest.raises(SecretChannelError, match="expected"):
        channel.send(ADMIN_START, password=bytearray(b"p"))
    with pytest.raises(SecretChannelError, match="not a code-only"):
        channel.send(OWNER_START, code=bytearray(b"123456"))
    channel.send(OWNER_START, password=bytearray(b"p"))
    with pytest.raises(SecretChannelError, match="not a password-bearing"):
        channel.send(OWNER_CONFIRM, password=bytearray(b"p"))
    with pytest.raises(SecretChannelError, match="exactly six bytes"):
        channel.send(OWNER_CONFIRM, code=bytearray(b"12345"))
    with pytest.raises(SecretChannelError, match="before sending every"):
        channel.close()
    os.close(read_fd)


def test_secret_channel_zeroizes_its_caller_buffer() -> None:
    read_fd, write_fd = os.pipe()
    channel = SecretChannel(write_fd)
    buffer = bytearray(b"Sy7-Synthetic-Owner-Buffer")
    channel.send(OWNER_START, password=buffer)

    assert bytes(buffer) == bytes(len(buffer))
    os.close(read_fd)
    os.close(write_fd)


def test_secret_channel_requires_a_private_descriptor() -> None:
    with pytest.raises(SecretChannelError, match="private descriptor"):
        SecretChannel(1)


def test_frame_codec_round_trips_and_rejects_truncation() -> None:
    framed = encode_frame(OWNER_START, 0, bytearray(b"synthetic"))
    assert decode_frame(bytes(framed)) == (OWNER_START, 0, b"synthetic")
    with pytest.raises(SecretChannelError):
        decode_frame(bytes(framed)[:-1])


def test_helper_sequence_rejects_order_reuse_and_residue() -> None:
    helpers = HelperSequence()
    with pytest.raises(HelperSequenceError, match="expected owner/pending-enrollment"):
        helpers.record(PHYSICIAN, PENDING, 1, CLEAN_OUTCOME)
    with pytest.raises(HelperSequenceError, match="never own a TOTP device"):
        helpers.record("receptionist", PENDING, 1, CLEAN_OUTCOME)
    helpers.record(OWNER, PENDING, 1, CLEAN_OUTCOME)
    helpers.record(OWNER, CONFIRMED, 10, CLEAN_OUTCOME)
    with pytest.raises(HelperSequenceError, match="reused a counter"):
        helpers.record(OWNER, CONFIRMED, 10, CLEAN_OUTCOME)
    with pytest.raises(HelperSequenceError, match="expected"):
        helpers.require_complete()


def test_helper_sequence_rejects_leaked_bytes_and_descriptors() -> None:
    for outcome, match in (
        (HelperOutcome(5, 0, 0, 0), "six bytes"),
        (HelperOutcome(6, 1, 0, 0), "diagnostic bytes"),
        (HelperOutcome(6, 0, 1, 0), "diagnostic bytes"),
        (HelperOutcome(6, 0, 0, 1), "inherited descriptor"),
    ):
        with pytest.raises(HelperSequenceError, match=match):
            HelperSequence().record(OWNER, PENDING, 1, outcome)
