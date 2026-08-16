# ruff: noqa: D100, D103, EM102, PLR2004, PTH108, PTH116, TRY003
# allow: SIZE_OK - approved byte-equivalent crash-safe proof state machine.
import datetime
import fcntl
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path, PurePosixPath
from typing import Never

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ops.testing.execution_host_resume_authority import (  # noqa: E402
    ResumePublicationInputs,
    validate_resume_publication_authority,
)


def fail(message: str) -> Never:
    raise SystemExit(f"execution-host-preflight: {message}")


def identity(path: Path, expected: str) -> dict[str, int]:
    value = os.stat(path, follow_symlinks=False)
    if expected == "directory" and not stat.S_ISDIR(value.st_mode):
        fail(f"{path} is not a directory")
    if expected == "regular" and not stat.S_ISREG(value.st_mode):
        fail(f"{path} is not a regular control file")
    return {
        "device": value.st_dev,
        "gid": value.st_gid,
        "inode": value.st_ino,
        "link_count": value.st_nlink,
        "mode": stat.S_IMODE(value.st_mode),
        "uid": value.st_uid,
    }


def canonical(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("utf-8")


def unescape_mount(value: str) -> str:
    for encoded, decoded in (
        ("\\134", "\\"),
        ("\\040", " "),
        ("\\011", "\t"),
        ("\\012", "\n"),
    ):
        value = value.replace(encoded, decoded)
    return value


FOUNDATION_SHA = "cffbb1900ae2132560f20c27fcf1a514a1ef71aa"


def parse_command() -> tuple[Path, Path | None, int | None]:
    arguments = sys.argv[1:]
    if len(arguments) == 3 and arguments[:2] == ["publish", "--authority-root"]:
        return Path(arguments[2]), None, None
    if (
        len(arguments) == 7
        and arguments[:2] == ["publish", "--authority-root"]
        and arguments[3] == "--stale-boot-resume"
        and arguments[5] == "--stable-lock-fd"
    ):
        try:
            descriptor = int(arguments[6])
        except ValueError:
            fail("stable lock descriptor is not an integer")
        if descriptor < 0:
            fail("stable lock descriptor is negative")
        return Path(arguments[2]), Path(arguments[4]), descriptor
    fail("invalid command grammar")


authority_root_arg, stale_resume_journal, inherited_lock_descriptor = parse_command()
if not authority_root_arg.is_absolute() or authority_root_arg.is_symlink():
    fail("authority root must be an absolute directory")
authority_root = authority_root_arg.resolve(strict=True)
if authority_root != authority_root_arg or authority_root.name != ".omo":
    fail("authority root is noncanonical")
workspace = authority_root.parent.resolve(strict=True)
foundation_sha = FOUNDATION_SHA
boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
if not re.fullmatch(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
    boot_id,
):
    fail("invalid boot ID")
artifact_arg = authority_root / f"clinic-os-phase1a-execution-host-{boot_id}.json"
plan = workspace / ".omo/plans/clinic-os-phase-1a-staff-scheduling.md"
if not plan.is_file() or plan.is_symlink():
    fail("invoked plan is not a regular workspace file")
omo = (workspace / ".omo").resolve(strict=True)
if omo != authority_root:
    fail("authority root is not the invoked workspace .omo")
if not omo.is_dir() or os.stat(omo).st_uid != os.geteuid():
    fail(".omo is not an executor-owned directory")
if artifact_arg.name != f"clinic-os-phase1a-execution-host-{boot_id}.json":
    fail("wrong proof basename")
artifact = artifact_arg.parent.resolve(strict=True) / artifact_arg.name
if artifact.parent != omo:
    fail("proof is outside the invoked workspace .omo directory")
pending = artifact.with_name(f".{artifact.name}.pending")

rows = Path("/proc/self/cgroup").read_text(encoding="ascii").splitlines()
if len(rows) != 1 or not rows[0].startswith("0::"):
    fail("expected one unified cgroup-v2 membership")
relative = rows[0][3:]
if (
    not relative.startswith("/")
    or "\x00" in relative
    or str(PurePosixPath(relative)) != relative
    or ".." in PurePosixPath(relative).parts
):
    fail("noncanonical cgroup path")
mount = Path("/sys/fs/cgroup").resolve(strict=True)
mount_rows = []
for row in Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines():
    left, separator, right = row.partition(" - ")
    left_fields = left.split()
    right_fields = right.split()
    if (
        separator
        and len(left_fields) >= 6
        and len(right_fields) >= 3
        and unescape_mount(left_fields[4]) == str(mount)
        and right_fields[0] == "cgroup2"
    ):
        mount_rows.append(row)
if len(mount_rows) != 1:
    fail("/sys/fs/cgroup is not the unique cgroup2 mount")
parent = (mount / relative.lstrip("/")).resolve(strict=True)
if parent != mount and mount not in parent.parents:
    fail("derived parent escaped the cgroup2 mount")
parent_identity = identity(parent, "directory")
if parent_identity["uid"] != os.geteuid() or parent_identity["gid"] != os.getegid():
    fail("derived parent is not executor-owned")
controls = {
    "cgroup_events_identity": (parent / "cgroup.events", os.R_OK),
    "cgroup_kill_identity": (parent / "cgroup.kill", os.W_OK),
    "cgroup_procs_identity": (parent / "cgroup.procs", os.W_OK),
}
control_identities: dict[str, dict[str, int]] = {}
if not os.access(parent, os.W_OK | os.X_OK, effective_ids=True):
    fail("derived parent is not writable/searchable")
for key, (path, access) in controls.items():
    control_identities[key] = identity(path, "regular")
    if not os.access(path, access, effective_ids=True):
        fail(f"required access denied for {path}")
core = {
    "boot_id": boot_id,
    "cgroup_mount_identity": identity(mount, "directory"),
    "cgroup_mount_path": str(mount),
    "cgroup_parent_identity": parent_identity,
    "cgroup_parent_path": str(parent),
    "cgroup_relative_path": relative,
    "foundation_sha": foundation_sha,
    "purpose": "clinic-os-phase1a-execution-host-cgroup-v2",
    "schema_version": 1,
    "workspace_realpath": str(workspace),
    **control_identities,
}


def validate_descriptor(
    descriptor: int, path: Path, allowed_links: set[int]
) -> tuple[bytes, os.stat_result]:
    file_identity = os.fstat(descriptor)
    if (
        not stat.S_ISREG(file_identity.st_mode)
        or stat.S_IMODE(file_identity.st_mode) != 0o400
        or file_identity.st_uid != os.geteuid()
        or file_identity.st_gid != os.getegid()
        or file_identity.st_nlink not in allowed_links
        or file_identity.st_size > 65536
    ):
        fail(f"invalid proof identity: {path}")
    os.lseek(descriptor, 0, os.SEEK_SET)
    raw = b""
    while True:
        chunk = os.read(descriptor, 65536 - len(raw) + 1)
        if not chunk:
            break
        raw += chunk
        if len(raw) > 65536:
            fail("proof exceeds 65536 bytes")
    try:
        parsed = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        fail(f"invalid proof JSON: {error}")
    if not isinstance(parsed, dict) or set(parsed) != set(core) | {"checked_at_utc"}:
        fail("proof has the wrong closed key set")
    checked = parsed.get("checked_at_utc")
    if not isinstance(checked, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z", checked
    ):
        fail("invalid proof timestamp")
    comparable = dict(parsed)
    comparable.pop("checked_at_utc")
    if comparable != core or canonical(parsed) != raw:
        fail("proof does not match the current host")
    return raw, file_identity


def validate(path: Path, allowed_links: set[int]) -> tuple[bytes, os.stat_result]:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        return validate_descriptor(descriptor, path, allowed_links)
    finally:
        os.close(descriptor)


canonical_ledger = omo / "evidence/isolation-ledger-phase1a.json"
stable_lock = omo / "evidence/isolation-ledger-phase1a.lock"
archive_state = omo / "evidence/isolation-archive-rollover-phase1a.json"
archive_sentinel = omo / "evidence/isolation-archive-rollover-phase1a.sentinel"


def present(path: Path) -> bool:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    return True


def same_pending_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return all(
        getattr(left, field) == getattr(right, field)
        for field in (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_uid",
            "st_gid",
            "st_nlink",
            "st_size",
        )
    )


def require_archive_quiet() -> None:
    if present(archive_state) or present(archive_sentinel):
        fail("archive rollover is active")


def validate_linked_pair(
    artifact_descriptor: int,
    pending_descriptor: int,
    failure_message: str,
) -> None:
    artifact_raw, artifact_identity = validate_descriptor(
        artifact_descriptor, artifact, {2}
    )
    pending_raw, pending_identity = validate_descriptor(
        pending_descriptor, pending, {2}
    )
    named_artifact_identity = os.stat(artifact, follow_symlinks=False)
    named_pending_identity = os.stat(pending, follow_symlinks=False)
    if (
        artifact_raw != pending_raw
        or not same_pending_identity(artifact_identity, pending_identity)
        or not same_pending_identity(artifact_identity, named_artifact_identity)
        or not same_pending_identity(artifact_identity, named_pending_identity)
    ):
        fail(failure_message)


def finish_linked_publication(
    artifact_descriptor: int,
    pending_descriptor: int,
    directory_descriptor: int,
) -> None:
    validate_linked_pair(
        artifact_descriptor,
        pending_descriptor,
        "pending proof does not authenticate the published proof",
    )
    require_archive_quiet()
    validate_linked_pair(
        artifact_descriptor,
        pending_descriptor,
        "proof hard-link cleanup identity changed",
    )
    os.unlink(pending)
    os.fsync(directory_descriptor)
    final_artifact_identity = os.stat(artifact, follow_symlinks=False)
    final_artifact_descriptor_identity = os.fstat(artifact_descriptor)
    final_pending_descriptor_identity = os.fstat(pending_descriptor)
    if (
        not same_pending_identity(
            final_artifact_identity,
            final_artifact_descriptor_identity,
        )
        or not same_pending_identity(
            final_artifact_identity,
            final_pending_descriptor_identity,
        )
        or stat.S_IMODE(final_artifact_identity.st_mode) != 0o400
        or final_artifact_identity.st_nlink != 1
        or present(pending)
    ):
        fail("proof hard-link cleanup did not converge")


if stale_resume_journal is None:
    if present(canonical_ledger):
        fail("ordinary proof publication requires an absent canonical ledger")
else:
    if not stale_resume_journal.is_absolute() or stale_resume_journal.is_symlink():
        fail("stale-resume journal must be an absolute regular file")
    journal_identity = os.stat(stale_resume_journal, follow_symlinks=False)
    if (
        not stat.S_ISREG(journal_identity.st_mode)
        or stat.S_IMODE(journal_identity.st_mode) != 0o600
        or journal_identity.st_uid != os.geteuid()
        or journal_identity.st_gid != os.getegid()
        or journal_identity.st_nlink != 1
        or not present(canonical_ledger)
        or inherited_lock_descriptor is None
    ):
        fail("invalid stale-resume publication authority")
    journal_raw = stale_resume_journal.read_bytes()
    ledger_raw = canonical_ledger.read_bytes()
    try:
        journal_value = json.loads(journal_raw)
        ledger_value = json.loads(ledger_raw)
        validate_resume_publication_authority(
            ResumePublicationInputs(
                journal_value,
                journal_raw,
                stale_resume_journal,
                ledger_value,
                ledger_raw,
                canonical_ledger,
                authority_root,
                boot_id,
                artifact,
            )
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        fail(str(error))
    attempt_root = Path(str(ledger_value["attempt_root"]))
    for relative in (
        "accepted-close-state.json",
        "rejection-spec.json",
        "terminal-revalidation",
        "user-fix-intent.json",
    ):
        if present(attempt_root / relative):
            fail("stale-resume attempt already has a terminal decision")


stable_lock_descriptor: int | None
try:
    if inherited_lock_descriptor is None:
        stable_lock_descriptor = os.open(
            stable_lock, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW
        )
    else:
        stable_lock_descriptor = os.dup(inherited_lock_descriptor)
except FileNotFoundError:
    stable_lock_descriptor = None
else:
    stable_lock_identity = os.fstat(stable_lock_descriptor)
    if (
        not stat.S_ISREG(stable_lock_identity.st_mode)
        or stat.S_IMODE(stable_lock_identity.st_mode) != 0o600
        or stable_lock_identity.st_uid != os.geteuid()
        or stable_lock_identity.st_gid != os.getegid()
        or stable_lock_identity.st_nlink != 1
        or stable_lock_identity.st_size != 0
    ):
        fail("invalid stable ledger lock")
    try:
        fcntl.flock(stable_lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fail("stable ledger lock is busy")
    locked_identity = os.fstat(stable_lock_descriptor)
    named_identity = os.stat(stable_lock, follow_symlinks=False)
    if (
        locked_identity.st_dev != named_identity.st_dev
        or locked_identity.st_ino != named_identity.st_ino
        or locked_identity.st_mode != named_identity.st_mode
        or locked_identity.st_uid != named_identity.st_uid
        or locked_identity.st_gid != named_identity.st_gid
        or locked_identity.st_nlink != named_identity.st_nlink
    ):
        fail("stable ledger lock identity changed")

require_archive_quiet()

try:
    os.lstat(artifact)
except FileNotFoundError:
    if present(canonical_ledger) and stale_resume_journal is None:
        fail("archive the prior canonical attempt before publishing a new-boot proof")
    try:
        descriptor = os.open(
            pending,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        created_pending = True
    except FileExistsError:
        descriptor = os.open(pending, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        created_pending = False
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fail("proof pending file has a live writer")
        pending_identity = os.fstat(descriptor)
        if (
            not stat.S_ISREG(pending_identity.st_mode)
            or stat.S_IMODE(pending_identity.st_mode) not in {0o600, 0o400}
            or pending_identity.st_uid != os.geteuid()
            or pending_identity.st_gid != os.getegid()
            or pending_identity.st_nlink != 1
            or pending_identity.st_size > 65536
        ):
            fail("invalid proof pending identity")
        if not same_pending_identity(
            pending_identity, os.stat(pending, follow_symlinks=False)
        ):
            fail("proof pending identity changed")
        directory = os.open(omo, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            if created_pending:
                os.fsync(directory)
            if stat.S_IMODE(pending_identity.st_mode) == 0o600:
                writer = descriptor
                close_writer = False
                if not created_pending:
                    writer = os.open(pending, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
                    close_writer = True
                try:
                    locked_identity = os.fstat(descriptor)
                    writer_identity = os.fstat(writer)
                    named_identity = os.stat(pending, follow_symlinks=False)
                    if not same_pending_identity(
                        locked_identity, writer_identity
                    ) or not same_pending_identity(locked_identity, named_identity):
                        fail("proof pending identity changed")
                    record = dict(core)
                    record["checked_at_utc"] = datetime.datetime.now(
                        datetime.UTC
                    ).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
                    raw = canonical(record)
                    os.ftruncate(writer, 0)
                    os.lseek(writer, 0, os.SEEK_SET)
                    view = memoryview(raw)
                    while view:
                        view = view[os.write(writer, view) :]
                    os.fsync(writer)
                    os.fchmod(writer, 0o400)
                    os.fsync(writer)
                finally:
                    if close_writer:
                        os.close(writer)
            pending_raw, pending_identity = validate(pending, {1})
            if not same_pending_identity(os.fstat(descriptor), pending_identity):
                fail("proof pending identity changed")
            require_archive_quiet()
            try:
                os.link(pending, artifact, follow_symlinks=False)
            except FileExistsError:
                artifact_raw, artifact_identity = validate(artifact, {2})
                if (
                    artifact_raw != pending_raw
                    or artifact_identity.st_dev != pending_identity.st_dev
                    or artifact_identity.st_ino != pending_identity.st_ino
                ):
                    fail("concurrent proof publication mismatch")
            os.fsync(directory)
            artifact_descriptor = os.open(
                artifact, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
            )
            try:
                finish_linked_publication(
                    artifact_descriptor,
                    descriptor,
                    directory,
                )
            finally:
                os.close(artifact_descriptor)
        finally:
            os.close(directory)
    finally:
        os.close(descriptor)
else:
    artifact_descriptor = os.open(artifact, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        try:
            pending_descriptor = os.open(
                pending, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
            )
        except FileNotFoundError:
            artifact_raw, artifact_identity = validate_descriptor(
                artifact_descriptor, artifact, {1}
            )
            named_artifact_identity = os.stat(artifact, follow_symlinks=False)
            if not same_pending_identity(artifact_identity, named_artifact_identity):
                fail("published proof identity changed")
        else:
            try:
                try:
                    fcntl.flock(pending_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    fail("proof pending file has a live writer")
                directory = os.open(omo, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
                try:
                    finish_linked_publication(
                        artifact_descriptor,
                        pending_descriptor,
                        directory,
                    )
                finally:
                    os.close(directory)
            finally:
                os.close(pending_descriptor)
    finally:
        os.close(artifact_descriptor)

require_archive_quiet()
final_raw, final_identity = validate(artifact, {1})
if present(pending):
    fail("proof pending file remains after publication")
print(hashlib.sha256(final_raw).hexdigest())  # noqa: T201
