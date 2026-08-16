from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
from typing import TYPE_CHECKING

import pytest

from execution_host_preflight_harness import PreflightHarness

if TYPE_CHECKING:
    from pathlib import Path

CRASH_STAGES = (
    "pending-created",
    "pending-directory-synced",
    "pending-reconstructed",
    "pending-data-synced",
    "pending-mode-immutable",
    "pending-metadata-synced",
    "proof-linked",
    "proof-directory-synced",
    "pending-unlinked",
    "cleanup-directory-synced",
)


def test_empty_publication_and_identical_replay_are_immutable(tmp_path: Path) -> None:
    # Given: an empty executor-owned proof namespace on a supported synthetic host.
    harness = PreflightHarness.create(tmp_path)

    # When: the exact committed publisher runs twice for the same boot and host.
    first_digest = harness.run()
    first_identity = harness.artifact.stat()
    first_raw = harness.artifact.read_bytes()
    second_digest = harness.run()

    # Then: both invocations authenticate one unchanged canonical immutable proof.
    assert first_digest == second_digest == hashlib.sha256(first_raw).hexdigest()
    assert harness.artifact.stat().st_ino == first_identity.st_ino
    assert stat.S_IMODE(harness.artifact.stat().st_mode) == 0o400
    assert harness.artifact.stat().st_nlink == 1
    assert not harness.pending.exists()
    assert json.loads(first_raw)["boot_id"].startswith("11111111-")


@pytest.mark.parametrize("crash_stage", CRASH_STAGES)
def test_every_pending_publication_prefix_replays(
    tmp_path: Path,
    crash_stage: str,
) -> None:
    # Given: one selected create/write/fsync/fchmod/link/unlink crash boundary.
    harness = PreflightHarness.create(tmp_path)

    def crash(stage: str) -> None:
        if stage == crash_stage:
            message = f"simulated proof crash after {stage}"
            raise RuntimeError(message)

    # When: publication crashes once and the exact command is replayed.
    with pytest.raises(RuntimeError, match="simulated proof crash"):
        harness.run(crash)
    digest = harness.run()

    # Then: replay converges to one mode-0400 link and no pending sibling.
    assert digest == hashlib.sha256(harness.artifact.read_bytes()).hexdigest()
    assert stat.S_IMODE(harness.artifact.stat().st_mode) == 0o400
    assert harness.artifact.stat().st_nlink == 1
    assert not harness.pending.exists()


def test_mode_0600_pending_is_reconstructed_on_the_locked_inode(
    tmp_path: Path,
) -> None:
    # Given: a crash-left private pending inode containing partial bytes.
    harness = PreflightHarness.create(tmp_path)
    harness.pending.write_bytes(b"partial")
    harness.pending.chmod(0o600)
    original_inode = harness.pending.stat().st_ino
    observed_lock = False

    def observe(stage: str) -> None:
        nonlocal observed_lock
        if stage != "pending-reconstructed":
            return
        competitor = os.open(
            harness.pending,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(competitor)
        observed_lock = True

    # When: the publisher resumes the private prefix.
    harness.run(observe)

    # Then: it rewrites and publishes that same exclusively locked inode.
    assert observed_lock
    assert harness.artifact.stat().st_ino == original_inode
    assert not harness.pending.exists()


def test_mode_0400_pending_is_directly_linked_without_writable_reopen(
    tmp_path: Path,
) -> None:
    # Given: canonical mode-0400 pending bytes recovered from a completed proof.
    harness = PreflightHarness.create(tmp_path)
    harness.run()
    raw = harness.artifact.read_bytes()
    harness.artifact.unlink()
    harness.pending.write_bytes(raw)
    harness.pending.chmod(0o400)
    original_inode = harness.pending.stat().st_ino

    def reject_writable_reopen(stage: str) -> None:
        if stage == "pending-writable-reopen":
            message = "immutable pending inode was reopened writable"
            raise AssertionError(message)

    # When: the committed publisher resumes after fchmod.
    harness.run(reject_writable_reopen)

    # Then: the exact pending inode is linked directly and cleaned up.
    assert harness.artifact.stat().st_ino == original_inode
    assert harness.artifact.read_bytes() == raw
    assert not harness.pending.exists()


@pytest.mark.parametrize(
    "crash_stage",
    ["pending-mode-immutable", "pending-metadata-synced"],
)
def test_both_post_fchmod_prefixes_replay_without_writable_reopen(
    tmp_path: Path,
    crash_stage: str,
) -> None:
    # Given: publication crashes on either side of the pending metadata fsync.
    harness = PreflightHarness.create(tmp_path)

    def crash(stage: str) -> None:
        if stage == crash_stage:
            message = "simulated post-fchmod crash"
            raise RuntimeError(message)

    with pytest.raises(RuntimeError, match="post-fchmod"):
        harness.run(crash)
    assert stat.S_IMODE(harness.pending.stat().st_mode) == 0o400

    def reject_writable_reopen(stage: str) -> None:
        if stage == "pending-writable-reopen":
            message = "post-fchmod replay reopened pending writable"
            raise AssertionError(message)

    # When / Then: replay directly hard-links the retained immutable inode.
    harness.run(reject_writable_reopen)
    assert not harness.pending.exists()


@pytest.mark.parametrize("marker", ["state", "sentinel"])
def test_existing_linked_prefix_rechecks_archive_marker_while_locked(
    tmp_path: Path,
    marker: str,
) -> None:
    # Given: a linked crash prefix and a marker injected after authentication.
    harness = PreflightHarness.create(tmp_path)
    harness.run()
    os.link(harness.artifact, harness.pending, follow_symlinks=False)
    inode = harness.artifact.stat().st_ino
    marker_path = (
        harness.archive_state if marker == "state" else harness.archive_sentinel
    )

    def inject(stage: str) -> None:
        if stage != "linked-descriptor-authenticated":
            return
        marker_path.write_bytes(b"active")
        competitor = os.open(harness.pending, os.O_RDONLY | os.O_CLOEXEC)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(competitor)

    # When: branch-local cleanup observes the newly present marker.
    with pytest.raises(SystemExit, match="archive rollover is active"):
        harness.run(inject)

    # Then: both immutable names remain unchanged while the pending lock was held.
    assert harness.artifact.stat().st_ino == harness.pending.stat().st_ino == inode
    assert harness.artifact.stat().st_nlink == harness.pending.stat().st_nlink == 2


def test_fresh_link_prefix_rechecks_archive_marker_before_unlink(
    tmp_path: Path,
) -> None:
    # Given: an archive marker appears only after the new hard link is authenticated.
    harness = PreflightHarness.create(tmp_path)

    def inject(stage: str) -> None:
        if stage == "linked-descriptor-authenticated":
            harness.archive_sentinel.write_bytes(b"active")

    # When: the new-publication branch reaches its final mutation gate.
    with pytest.raises(SystemExit, match="archive rollover is active"):
        harness.run(inject)

    # Then: branch-local refusal retains both names for locked recovery.
    assert harness.artifact.stat().st_ino == harness.pending.stat().st_ino
    assert harness.artifact.stat().st_nlink == harness.pending.stat().st_nlink == 2


def test_pending_substitution_before_final_revalidation_is_not_unlinked(
    tmp_path: Path,
) -> None:
    # Given: a valid linked prefix whose pending name changes after authentication.
    harness = PreflightHarness.create(tmp_path)
    harness.run()
    os.link(harness.artifact, harness.pending, follow_symlinks=False)
    artifact_raw = harness.artifact.read_bytes()

    def substitute(stage: str) -> None:
        if stage != "linked-prefix-authenticated":
            return
        harness.pending.unlink()
        harness.pending.write_bytes(b"foreign")
        harness.pending.chmod(0o400)

    # When: final retained descriptor/name validation observes the substitution.
    with pytest.raises(
        SystemExit,
        match=r"identity changed|invalid proof identity",
    ):
        harness.run(substitute)

    # Then: the publisher leaves the foreign name and authentic artifact untouched.
    assert harness.pending.read_bytes() == b"foreign"
    assert harness.artifact.read_bytes() == artifact_raw


def test_nonblocking_stable_lock_serializes_a_competing_publisher(
    tmp_path: Path,
) -> None:
    # Given: the stable lock exists and one publisher holds it during reconstruction.
    harness = PreflightHarness.create(tmp_path)
    harness.create_stable_lock()
    observed_contention = False

    def contend(stage: str) -> None:
        nonlocal observed_contention
        if stage != "pending-data-synced":
            return
        with pytest.raises(SystemExit, match="stable ledger lock is busy"):
            harness.run()
        observed_contention = True

    # When: the first publisher completes and its contender retries afterward.
    first = harness.run(contend)
    second = harness.run()

    # Then: contention is explicit and identical replay converges.
    assert observed_contention
    assert first == second


def test_busy_pending_writer_and_different_host_fail_unchanged(tmp_path: Path) -> None:
    # Given: first a live pending writer, then one completed current-host proof.
    harness = PreflightHarness.create(tmp_path)
    harness.pending.write_bytes(b"partial")
    harness.pending.chmod(0o600)
    descriptor = os.open(harness.pending, os.O_RDONLY | os.O_CLOEXEC)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(SystemExit, match="proof pending file has a live writer"):
            harness.run()
        assert harness.pending.read_bytes() == b"partial"
    finally:
        os.close(descriptor)
    harness.run()
    raw = harness.artifact.read_bytes()
    (harness.cgroup_parent / "cgroup.events").chmod(0o600)

    # When / Then: host-identity drift rejects reuse without rewriting the proof.
    with pytest.raises(SystemExit, match="proof does not match the current host"):
        harness.run()
    assert harness.artifact.read_bytes() == raw
