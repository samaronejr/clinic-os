"""Capture, materialize, and verify the current worktree source identity.

The manifest identifies the bytes captured at snapshot time; the Git HEAD and
tree are recorded as provenance only. The legacy clean-revision gate in
``image_source`` and the frozen-plan authentication in
``tracked_approved_plan`` keep their exact historical behavior.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Final, Never, cast

import rfc8785

from ops.testing.approved_plan import ApprovedPlan, verify_plan
from ops.testing.image_builder import build_candidate_image
from ops.testing.image_source import (
    REQUIRED_CONTEXT_TARGETS,
    candidate_available_suites,
    candidate_context_target,
)
from ops.testing.isolation_common import (
    MODE_DIRECTORY,
    MODE_IMMUTABLE,
    MODE_PRIVATE,
    IsolationError,
    JsonObject,
    JsonValue,
    canonical_bytes,
    directory_identity,
    load_json,
    raw_sha256,
    utc_now,
    write_no_replace,
)
from ops.testing.isolation_ledger_store import (
    BOOT_ID_PATH,
    locked_open_ledger,
    locked_stale_ledger,
)
from ops.testing.isolation_snapshot import LEDGER_NAME
from ops.testing.runtime_paths import runtime_directory
from ops.testing.tracked_approved_plan import authenticate_tracked_plan

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from contextlib import AbstractContextManager

    from ops.testing.isolation_ledger_store import LedgerSession

GIT: Final = shutil.which("git")
DOCKER: Final = shutil.which("docker")
SHA40: Final = re.compile(r"[0-9a-f]{40}")
SHA64: Final = re.compile(r"[0-9a-f]{64}")
IMAGE_ID: Final = re.compile(r"sha256:[0-9a-f]{64}")
READ_CHUNK: Final = 1024 * 1024
COMMAND_TIMEOUT_SECONDS: Final = 30
MAX_OUTPUT_BYTES: Final = 16 * 1024 * 1024
MANIFEST_NAME: Final = "current-source-manifest.json"
PAYLOAD_NAME: Final = "payload"
RUNTIME_SUBTREE: Final = "clinic-os-phase1a-runtime"
CONTEXT_KINDS: Final = ("application", "browser-runner")
EXCLUDED_COMPONENTS: Final = frozenset(
    {
        ".agents",
        ".claude",
        ".codex",
        ".git",
        ".omo",
        ".omx",
        ".venv",
        "__pycache__",
        "evidence",
        "node_modules",
        "venv",
    }
)
EXCLUDED_NAMES: Final = frozenset(
    {
        ".env",
        ".env.local",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "credentials",
        "credentials.json",
        "id_rsa",
        "id_ed25519",
    }
)
EXCLUDED_SUFFIXES: Final = (".pem", ".key", ".p12", ".pfx")
INDEX_FIELD_COUNT: Final = 3
RECORD_KEYS: Final = frozenset(
    {
        "approved_plan_sha256",
        "attempt_id",
        "attempt_root",
        "base_revision_sha",
        "base_tree_sha",
        "built_at_utc",
        "contexts",
        "images",
        "ledger_sha256",
        "repository",
        "schema_version",
        "snapshot_manifest",
        "snapshot_manifest_sha256",
    }
)
_SNAPSHOT_OPTIONS: Final = frozenset(
    {"--allowlist", "--report", "--repository", "--run-root"}
)
_VERIFY_OPTIONS: Final = frozenset({"--record", "--repository", "--run-root"})


def capture_current_source(
    repository: Path,
    staging: Path,
    *,
    authorized_untracked: tuple[str, ...] = (),
) -> tuple[JsonObject, str]:
    """Capture current tracked bytes plus authorized untracked files.

    ``staging`` must be an existing executor-owned private directory; payload
    copies and the manifest are staged beneath it. Git conflicts, symlinks,
    special files, path escapes, target collisions, and source replacement
    during the read are all rejected.
    """
    root = _canonical_repository(repository)
    _require_private_directory(staging, "snapshot staging")
    authorized = _authorized_paths(authorized_untracked)
    with _open_ledger(root / ".omo" / "evidence" / LEDGER_NAME) as session:
        ledger = session.ledger
        ledger_sha256 = raw_sha256(session.original_raw)
        attempt_id = _text(ledger.get("attempt_id"), "ledger attempt")
        attempt_root = _text(ledger.get("attempt_root"), "ledger attempt root")
        plan = _authenticate_plan(ledger, root)
    head = _git_text(root, "rev-parse", "HEAD")
    tree = _git_text(root, "rev-parse", "HEAD^{tree}")
    if SHA40.fullmatch(head) is None or SHA40.fullmatch(tree) is None:
        _fail("current HEAD or tree identity is invalid")
    tracked = _tracked_index(root)
    deleted = _deleted_paths(root)
    untracked = _untracked_paths(root)
    payload = staging / PAYLOAD_NAME
    if payload.exists() or payload.is_symlink():
        _fail("snapshot staging payload already exists")
    payload.mkdir(mode=MODE_DIRECTORY)
    entries: list[JsonObject] = []
    for path, index_mode in tracked:
        if path in deleted or _excluded(path):
            continue
        raw, mode = _read_stable(root, path, index_mode)
        _stage_payload(payload, path, raw)
        entries.append(
            {
                "git_mode": index_mode,
                "mode": mode,
                "path": path,
                "sha256": raw_sha256(raw),
                "size_bytes": len(raw),
                "source": "tracked",
            }
        )
    for path in sorted(authorized):
        if path in untracked:
            raw, mode = _read_stable(root, path, None)
            _stage_payload(payload, path, raw)
            entries.append(
                {
                    "git_mode": None,
                    "mode": mode,
                    "path": path,
                    "sha256": raw_sha256(raw),
                    "size_bytes": len(raw),
                    "source": "untracked",
                }
            )
        elif not (root / path).exists() and not (root / path).is_symlink():
            _fail(f"authorized untracked path is absent: {path}")
        else:
            _fail(f"authorized untracked path is not untracked: {path}")
    entries.sort(key=lambda item: str(item["path"]))
    manifest: JsonObject = {
        "approved_plan_sha256": plan.sha256,
        "attempt_id": attempt_id,
        "attempt_root": attempt_root,
        "authorized_untracked_paths": _json_strings(sorted(authorized)),
        "base_revision_sha": head,
        "base_tree_sha": tree,
        "contexts": _context_digests(entries, head, tree),
        "deleted_tracked_paths": _json_strings(sorted(deleted)),
        "entries": cast("list[JsonValue]", entries),
        "ledger_sha256": ledger_sha256,
        "repository": str(root),
        "schema_version": 1,
        "unauthorized_untracked_count": len(untracked - authorized),
    }
    manifest_raw = rfc8785.dumps(manifest) + b"\n"
    write_no_replace(staging / MANIFEST_NAME, manifest_raw, mode=MODE_IMMUTABLE)
    return manifest, raw_sha256(manifest_raw)


def materialize_current_context(
    staging: Path,
    manifest: JsonObject,
    kind: str,
    destination: Path,
) -> JsonObject:
    """Project one candidate context from the staged captured bytes."""
    if kind not in CONTEXT_KINDS or destination.exists() or destination.is_symlink():
        _fail("current-source context destination is invalid")
    entries = _manifest_entries(manifest)
    planned: dict[str, tuple[JsonObject, bytes]] = {}
    for entry in entries:
        source_path = _text(entry["path"], "entry path")
        target = candidate_context_target(kind, source_path)
        if target is None:
            continue
        if target in planned:
            _fail("current-source context target collision")
        staged = _staged_payload(staging, source_path)
        raw = staged.read_bytes()
        if len(raw) != _integer(entry["size_bytes"], "entry size") or raw_sha256(
            raw
        ) != _text(entry["sha256"], "entry digest"):
            _fail("staged payload bytes drifted from the snapshot manifest")
        planned[target] = (entry, raw)
    if not set(planned) >= REQUIRED_CONTEXT_TARGETS:
        _fail("current-source context lacks required inputs")
    destination.mkdir(mode=MODE_DIRECTORY)
    context_entries: list[JsonValue] = []
    for target in sorted(planned):
        entry, raw = planned[target]
        output = destination / target
        output.parent.mkdir(mode=MODE_DIRECTORY, parents=True, exist_ok=True)
        output.write_bytes(raw)
        mode = _text(entry["mode"], "entry mode")
        output.chmod(0o755 if mode == "100755" else 0o644)
        context_entries.append(
            {
                "git_mode": mode,
                "path": target,
                "sha256": raw_sha256(raw),
                "size_bytes": len(raw),
                "source_path": _text(entry["path"], "entry path"),
            }
        )
    context_manifest = _context_manifest(
        context_entries,
        kind,
        _text(manifest["base_revision_sha"], "base revision"),
        _text(manifest["base_tree_sha"], "base tree"),
    )
    manifest_raw = rfc8785.dumps(context_manifest) + b"\n"
    (destination / f"{kind}-source-manifest.json").write_bytes(manifest_raw)
    return {
        "available_suite_ids": _json_strings(
            candidate_available_suites(
                kind, {_text(entry["path"], "entry path") for entry in entries}
            )
        ),
        "kind": kind,
        "revision_sha": _text(manifest["base_revision_sha"], "base revision"),
        "source_entry_count": len(context_entries),
        "source_manifest_sha256": raw_sha256(manifest_raw),
        "tree_sha": _text(manifest["base_tree_sha"], "base tree"),
    }


def build_current_source_record(  # noqa: PLR0913 - closed record inputs.
    repository: Path,
    run_root: Path,
    report_path: Path,
    *,
    authorized_untracked: tuple[str, ...] = (),
    kinds: tuple[str, ...] = (),
    builders: dict[str, Callable[[Path, JsonObject, Path], None]] | None = None,
) -> JsonObject:
    """Snapshot, optionally build, and publish one current-source record.

    Staging lives under the private ``run_root`` and is removed before the
    immutable report is published.
    """
    for kind in kinds:
        if kind not in CONTEXT_KINDS:
            _fail("current-source build kind is invalid")
    _require_report_destination(report_path)
    with runtime_directory(run_root, purpose="current-source") as staging:
        manifest, manifest_digest = capture_current_source(
            repository,
            staging,
            authorized_untracked=authorized_untracked,
        )
        contexts: dict[str, JsonValue] = {}
        images: dict[str, JsonValue] = {}
        for kind in kinds:
            context = staging / f"context-{kind}"
            contract = materialize_current_context(staging, manifest, kind, context)
            contexts[kind] = {
                "context_manifest_sha256": contract["source_manifest_sha256"],
                "contract": contract,
            }
            builder = build_candidate_image if builders is None else builders.get(kind)
            if builder is None:
                _fail(f"current-source builder for {kind} is unavailable")
            iidfile = staging / f"{kind}-image-id"
            builder(context, contract, iidfile)
            image_id = iidfile.read_text(encoding="ascii").strip()
            if IMAGE_ID.fullmatch(image_id) is None:
                _fail("built image identity is invalid")
            images[kind] = image_id
        record: JsonObject = {
            "approved_plan_sha256": _text(
                manifest["approved_plan_sha256"], "plan digest"
            ),
            "attempt_id": _text(manifest["attempt_id"], "attempt"),
            "attempt_root": _text(manifest["attempt_root"], "attempt root"),
            "base_revision_sha": _text(manifest["base_revision_sha"], "base revision"),
            "base_tree_sha": _text(manifest["base_tree_sha"], "base tree"),
            "built_at_utc": utc_now(),
            "contexts": contexts,
            "images": images,
            "ledger_sha256": _text(manifest["ledger_sha256"], "ledger digest"),
            "repository": _text(manifest["repository"], "repository"),
            "schema_version": 1,
            "snapshot_manifest": manifest,
            "snapshot_manifest_sha256": manifest_digest,
        }
        if set(record) != RECORD_KEYS:
            _fail("current-source record keys drifted")
        raw = canonical_bytes(record)
    write_no_replace(report_path, raw, mode=MODE_IMMUTABLE)
    return record


def verify_current_source_record(
    repository: Path,
    record_path: Path,
    run_root: Path,
    *,
    image_inspector: Callable[[str], JsonObject] | None = None,
) -> JsonObject:
    """Re-authenticate a published record against the live worktree.

    The repository, ledger, frozen plan, and full source manifest are
    re-captured and compared; every recorded image is freshly inspected.
    """
    root = _canonical_repository(repository)
    record, _ = load_json(record_path)
    if set(record) != RECORD_KEYS:
        _fail("current-source record has the wrong closed shape")
    if record.get("repository") != str(root):
        _fail("current-source record repository differs")
    if record.get("base_revision_sha") != _git_text(root, "rev-parse", "HEAD"):
        _fail("current-source record base revision is stale")
    if record.get("base_tree_sha") != _git_text(root, "rev-parse", "HEAD^{tree}"):
        _fail("current-source record base tree is stale")
    manifest = _object(record["snapshot_manifest"], "snapshot manifest")
    if (
        raw_sha256(rfc8785.dumps(manifest) + b"\n")
        != record["snapshot_manifest_sha256"]
    ):
        _fail("current-source record manifest digest differs")
    authorized = _strings(
        manifest.get("authorized_untracked_paths"), "authorized untracked"
    )
    with runtime_directory(run_root, purpose="current-source-verify") as staging:
        fresh, fresh_digest = capture_current_source(
            root,
            staging,
            authorized_untracked=tuple(authorized),
        )
    if (
        fresh_digest != record["snapshot_manifest_sha256"]
        or fresh["ledger_sha256"] != record["ledger_sha256"]
        or fresh["approved_plan_sha256"] != record["approved_plan_sha256"]
        or fresh["attempt_id"] != record["attempt_id"]
        or fresh["attempt_root"] != record["attempt_root"]
    ):
        _fail("current-source record is stale or drifted")
    inspector = _inspect_image if image_inspector is None else image_inspector
    images = _object(record["images"], "record images")
    contexts = _object(record["contexts"], "record contexts")
    manifest_contexts = _object(manifest.get("contexts"), "manifest contexts")
    for kind, image_value in images.items():
        image_id = _text(image_value, "record image")
        context = _object(contexts.get(kind), "record context")
        contract = _object(context.get("contract"), "record image contract")
        expected = _object(manifest_contexts.get(kind), "manifest context digest")
        if contract.get("source_manifest_sha256") != expected.get(
            "manifest_sha256"
        ) or context.get("context_manifest_sha256") != expected.get("manifest_sha256"):
            _fail("current-source record context binding differs")
        _verify_image(image_id, contract, inspector)
    return record


def expected_image_labels(contract: JsonObject) -> dict[str, str]:
    """Project the exact image labels baked by the candidate Dockerfiles."""
    kind = _text(contract.get("kind"), "image kind")
    labels = {
        "clinic.phase1a.image-kind": kind,
        "clinic.phase1a.tree": _text(contract.get("tree_sha"), "image tree"),
        "org.opencontainers.image.revision": _text(
            contract.get("revision_sha"), "image revision"
        ),
    }
    manifest = _text(contract.get("source_manifest_sha256"), "image source manifest")
    count = _integer(contract.get("source_entry_count"), "image entry count")
    if kind == "application":
        labels["clinic.phase1a.application-source-sha256"] = manifest
        labels["clinic.phase1a.application-source-entry-count"] = str(count)
    elif kind == "browser-runner":
        labels["clinic.phase1a.runner-source-sha256"] = manifest
        labels["clinic.phase1a.runner-source-entry-count"] = str(count)
        labels["clinic.phase1a.available-suite-ids"] = ",".join(
            _strings(contract.get("available_suite_ids"), "image suites")
        )
    else:
        _fail("current-source image kind is invalid")
    return labels


def main(argv: list[str] | None = None) -> int:
    """Run the closed snapshot/build/verify command grammar."""
    arguments = sys.argv[1:] if argv is None else argv
    try:
        _dispatch(arguments)
    except (IsolationError, OSError, ValueError) as error:
        sys.stderr.write(f"current-source: {error}\n")
        return 2
    return 0


def _dispatch(arguments: list[str]) -> None:
    if not arguments:
        _fail("invalid current-source command grammar")
    operation, rest = arguments[0], arguments[1:]
    options = _options(rest)
    if operation in {"snapshot", "build"}:
        if not set(options) <= _SNAPSHOT_OPTIONS:
            _fail("invalid current-source command grammar")
        record = build_current_source_record(
            Path(_required(options, "--repository")),
            Path(_required(options, "--run-root")),
            Path(_required(options, "--report")),
            authorized_untracked=_allowlist(options.get("--allowlist")),
            kinds=() if operation == "snapshot" else CONTEXT_KINDS,
        )
        _print_summary(record)
        return
    if operation == "verify":
        if not set(options) <= _VERIFY_OPTIONS:
            _fail("invalid current-source command grammar")
        record = verify_current_source_record(
            Path(_required(options, "--repository")),
            Path(_required(options, "--record")),
            Path(_required(options, "--run-root")),
        )
        _print_summary(record)
        return
    _fail("invalid current-source command grammar")


def _options(arguments: list[str]) -> dict[str, str]:
    options: dict[str, str] = {}
    index = 0
    while index < len(arguments):
        name = arguments[index]
        if (
            not name.startswith("--")
            or index + 1 >= len(arguments)
            or arguments[index + 1].startswith("--")
            or name in options
        ):
            _fail("invalid current-source command grammar")
        options[name] = arguments[index + 1]
        index += 2
    return options


def _required(options: dict[str, str], name: str) -> str:
    value = options.get(name)
    if value is None or not value:
        _fail("invalid current-source command grammar")
    return value


def _allowlist(value: str | None) -> tuple[str, ...]:
    if value is None:
        return ()
    raw = Path(value).read_bytes()
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as error:
        message = "current-source allowlist is not UTF-8"
        raise IsolationError(message) from error
    if any(not line or line != line.strip() for line in lines):
        _fail("current-source allowlist is malformed")
    return tuple(lines)


def _print_summary(record: JsonObject) -> None:
    manifest = _object(record["snapshot_manifest"], "snapshot manifest")
    summary: JsonObject = {
        "attempt_id": record["attempt_id"],
        "base_revision_sha": record["base_revision_sha"],
        "entry_count": len(_manifest_entries(manifest)),
        "images": record["images"],
        "snapshot_manifest_sha256": record["snapshot_manifest_sha256"],
    }
    sys.stdout.write(canonical_bytes(summary).decode())


def _open_ledger(path: Path) -> AbstractContextManager[LedgerSession]:
    """Select the matching boot relation; the lock re-authenticates everything."""
    # ``.omo`` is a workspace symlink under the CI evidence binding; every
    # sibling caller resolves it before opening, so the recorded canonical
    # ledger path is what gets authenticated.
    path = path.resolve(strict=True)
    ledger, _ = load_json(path)
    if ledger.get("boot_id") == BOOT_ID_PATH.read_text(encoding="ascii").strip():
        return locked_open_ledger(path)
    return locked_stale_ledger(path)


def _canonical_repository(repository: Path) -> Path:
    if not repository.is_absolute() or repository.is_symlink():
        _fail("current-source repository must be an absolute canonical path")
    resolved = repository.resolve(strict=True)
    if resolved != repository or not stat.S_ISDIR(repository.stat().st_mode):
        _fail("current-source repository is noncanonical")
    directory_identity(resolved)
    top_level = _git_text(resolved, "rev-parse", "--show-toplevel")
    try:
        git_root = Path(top_level).resolve(strict=True)
    except (OSError, UnicodeDecodeError) as error:
        message = "current-source repository root is not canonical"
        raise IsolationError(message) from error
    if git_root != resolved:
        _fail("current-source repository is not the Git checkout root")
    return resolved


def _authenticate_plan(ledger: JsonObject, root: Path) -> ApprovedPlan:
    plan = _object(ledger.get("approved_plan"), "ledger approved plan")
    kind = _text(plan.get("source_kind"), "approved-plan source kind")
    path = Path(_text(plan.get("path"), "approved-plan path"))
    sidecar = Path(_text(plan.get("sidecar_path"), "approved-plan sidecar"))
    sha256 = _text(plan.get("sha256"), "approved-plan digest")
    if SHA64.fullmatch(sha256) is None:
        _fail("approved-plan digest is invalid")
    if kind == "tracked-ci":
        authenticated = authenticate_tracked_plan(path, sidecar, root)
    elif kind == "invoked":
        authenticated = ApprovedPlan(
            source_kind=kind,
            path=path,
            sha256=sha256,
            sidecar_path=sidecar,
        )
        verify_plan(
            authenticated,
            root / "docs/plans/clinic-os-phase1a-approved.md",
            root / "docs/plans/clinic-os-phase1a-approved.sha256",
        )
    else:
        _fail("approved-plan source kind is not authenticated")
    if authenticated.sha256 != sha256 or authenticated.as_json() != plan:
        _fail("approved-plan authentication disagrees with the ledger")
    return authenticated


def _tracked_index(root: Path) -> list[tuple[str, str]]:
    raw = _git_bytes(root, "ls-files", "-s", "-z")
    result: list[tuple[str, str]] = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        metadata, separator, encoded = record.partition(b"\t")
        fields = metadata.split(b" ")
        if not separator or len(fields) != INDEX_FIELD_COUNT:
            _fail("current-source index record is malformed")
        mode, stage = fields[0], fields[2]
        if stage != b"0":
            _fail("current-source index has unresolved conflicts")
        if mode not in {b"100644", b"100755"}:
            _fail("current-source index entry is not a regular file")
        result.append((_safe_relative(encoded), mode.decode("ascii")))
    return result


def _deleted_paths(root: Path) -> set[str]:
    raw = _git_bytes(root, "ls-files", "-d", "-z")
    return {_safe_relative(record) for record in raw.split(b"\0") if record}


def _untracked_paths(root: Path) -> set[str]:
    raw = _git_bytes(root, "ls-files", "--others", "--exclude-standard", "-z")
    return {_safe_relative(record) for record in raw.split(b"\0") if record}


def _safe_relative(encoded: bytes) -> str:
    try:
        path = encoded.decode("utf-8")
    except UnicodeDecodeError as error:
        message = "current-source path is not UTF-8"
        raise IsolationError(message) from error
    pure = PurePosixPath(path)
    if not path or pure.is_absolute() or ".." in pure.parts or str(pure) != path:
        _fail("current-source path escapes the repository")
    return path


def _authorized_paths(values: tuple[str, ...]) -> set[str]:
    result: set[str] = set()
    for value in values:
        path = _safe_relative(value.encode("utf-8"))
        if _excluded(path):
            _fail(f"authorized untracked path is excluded: {path}")
        if path in result:
            _fail(f"authorized untracked path is duplicated: {path}")
        result.add(path)
    return result


def _excluded(path: str) -> bool:
    return any(_excluded_component(part) for part in PurePosixPath(path).parts)


def _excluded_component(name: str) -> bool:
    if name in EXCLUDED_COMPONENTS or name in EXCLUDED_NAMES:
        return True
    if name.startswith(".env.") and name != ".env.example":
        return True
    return name.endswith(EXCLUDED_SUFFIXES)


def _read_stable(root: Path, path: str, index_mode: str | None) -> tuple[bytes, str]:
    leaf = PurePosixPath(path).name
    try:
        with _source_parent(root, path) as parent:
            named_before = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
            _require_source_identity(named_before, path)
            descriptor = os.open(
                leaf,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=parent,
            )
    except OSError as error:
        message = f"current-source file cannot be opened: {path}"
        raise IsolationError(message) from error
    try:
        before = os.fstat(descriptor)
        if _identity(before) != _identity(named_before):
            _fail(f"current-source file changed during capture: {path}")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, READ_CHUNK):
            chunks.append(chunk)
        after = os.fstat(descriptor)
        try:
            with _source_parent(root, path) as parent:
                named_after = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
        except OSError as error:
            message = f"current-source file cannot be opened: {path}"
            raise IsolationError(message) from error
    finally:
        os.close(descriptor)
    if _identity(after) != _identity(before) or _identity(named_after) != _identity(
        before
    ):
        _fail(f"current-source file changed during capture: {path}")
    mode = "100755" if stat.S_IMODE(before.st_mode) & 0o111 else "100644"
    if index_mode is not None and index_mode != mode:
        _fail(f"current-source file mode disagrees with the index: {path}")
    return b"".join(chunks), mode


@contextmanager
def _source_parent(root: Path, path: str) -> Iterator[int]:
    """Open the leaf's parent through a no-follow descriptor chain.

    Every ancestor component is opened with ``O_DIRECTORY|O_NOFOLLOW`` relative
    to the previous descriptor, so a symlinked or replaced ancestor can never
    redirect the read outside the canonical repository root.
    """
    parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        for part in PurePosixPath(path).parts[:-1]:
            descriptor = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=parent,
            )
            os.close(parent)
            parent = descriptor
        yield parent
    finally:
        os.close(parent)


def _require_source_identity(value: os.stat_result, path: str) -> None:
    mode = stat.S_IMODE(value.st_mode)
    if (
        not stat.S_ISREG(value.st_mode)
        or value.st_uid != os.geteuid()
        or value.st_gid != os.getegid()
        or value.st_nlink != 1
        or mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX)
    ):
        _fail(f"current-source file identity is invalid: {path}")


def _identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_uid,
        value.st_gid,
        value.st_nlink,
        value.st_size,
    )


def _stage_payload(payload: Path, path: str, raw: bytes) -> None:
    destination = payload / path
    destination.parent.mkdir(mode=MODE_DIRECTORY, parents=True, exist_ok=True)
    write_no_replace(destination, raw, mode=MODE_PRIVATE)


def _staged_payload(staging: Path, path: str) -> Path:
    candidate = staging / PAYLOAD_NAME / path
    if candidate.is_symlink() or not candidate.is_file():
        _fail("staged payload entry is missing or replaced")
    return candidate


def _context_manifest(
    entries: list[JsonValue],
    kind: str,
    revision: str,
    tree: str,
) -> JsonObject:
    return {
        "entries": entries,
        "kind": kind,
        "revision_sha": revision,
        "schema_version": 1,
        "tree_sha": tree,
    }


def _context_digests(entries: list[JsonObject], head: str, tree: str) -> JsonObject:
    digests: JsonObject = {}
    for kind in CONTEXT_KINDS:
        projected: list[JsonObject] = []
        for entry in entries:
            source_path = _text(entry["path"], "entry path")
            target = candidate_context_target(kind, source_path)
            if target is None:
                continue
            projected.append(
                {
                    "git_mode": _text(entry["mode"], "entry mode"),
                    "path": target,
                    "sha256": _text(entry["sha256"], "entry digest"),
                    "size_bytes": _integer(entry["size_bytes"], "entry size"),
                    "source_path": source_path,
                }
            )
        projected.sort(key=lambda item: str(item["path"]))
        context_manifest = _context_manifest(
            cast("list[JsonValue]", projected), kind, head, tree
        )
        digests[kind] = {
            "entry_count": len(projected),
            "manifest_sha256": raw_sha256(rfc8785.dumps(context_manifest) + b"\n"),
        }
    return digests


def _manifest_entries(manifest: JsonObject) -> list[JsonObject]:
    entries = manifest.get("entries")
    if not isinstance(entries, list):
        _fail("snapshot manifest entries are invalid")
    result: list[JsonObject] = []
    for entry in entries:
        if not isinstance(entry, dict):
            _fail("snapshot manifest entries are invalid")
        result.append(entry)
    return result


def _verify_image(
    image_id: str,
    contract: JsonObject,
    inspector: Callable[[str], JsonObject],
) -> None:
    if IMAGE_ID.fullmatch(image_id) is None:
        _fail("current-source image identity is invalid")
    inspection = inspector(image_id)
    if inspection.get("Id") != image_id or inspection.get("Architecture") != "amd64":
        _fail("current-source image identity or architecture drifted")
    config = _object(inspection.get("Config"), "image config")
    labels = config.get("Labels")
    if not isinstance(labels, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in labels.items()
    ):
        _fail("current-source image labels are invalid")
    expected = expected_image_labels(contract)
    if any(labels.get(name) != value for name, value in expected.items()):
        _fail("current-source image labels do not match the record")


def _inspect_image(image_id: str) -> JsonObject:
    if DOCKER is None:
        _fail("docker executable is unavailable")
    raw = _run(
        (
            DOCKER,
            "image",
            "inspect",
            "--format",
            "{{json .}}",
            image_id,
        )
    )
    try:
        value: JsonValue = json.loads(raw)
    except json.JSONDecodeError as error:
        message = "current-source image inspect returned invalid JSON"
        raise IsolationError(message) from error
    return _object(value, "current-source image inspect")


def _require_private_directory(path: Path, label: str) -> None:
    if not path.is_absolute() or path.is_symlink():
        _fail(f"{label} must be an absolute non-symlink directory")
    if path.resolve(strict=True) != path:
        _fail(f"{label} is noncanonical")
    identity = directory_identity(path)
    if identity["mode"] != MODE_DIRECTORY:
        _fail(f"{label} must have mode 0700")


def _require_report_destination(path: Path) -> None:
    if not path.is_absolute() or path.is_symlink() or path.exists():
        _fail("current-source report destination is invalid")
    _require_private_directory(path.parent, "current-source report parent")


def _git_text(root: Path, *arguments: str) -> str:
    try:
        return _git_bytes(root, *arguments).decode("ascii").strip()
    except UnicodeDecodeError as error:
        message = "current-source Git output is not ASCII"
        raise IsolationError(message) from error


def _git_bytes(root: Path, *arguments: str) -> bytes:
    if GIT is None:
        _fail("git executable is unavailable")
    return _run((GIT, "-C", str(root), *arguments))


def _run(command: tuple[str, ...]) -> bytes:
    try:
        completed = subprocess.run(  # noqa: S603 - fixed executable, closed argv.
            command,
            check=False,
            capture_output=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        message = "current-source command timed out"
        raise IsolationError(message) from error
    if (
        len(completed.stdout) > MAX_OUTPUT_BYTES
        or len(completed.stderr) > MAX_OUTPUT_BYTES
    ):
        _fail("current-source command output is too large")
    if completed.returncode != 0:
        _fail("current-source command failed")
    return completed.stdout


def _object(value: JsonValue, context: str) -> JsonObject:
    if not isinstance(value, dict):
        _fail(f"{context} must be an object")
    return value


def _text(value: JsonValue, context: str) -> str:
    if not isinstance(value, str):
        _fail(f"{context} must be a string")
    return value


def _integer(value: JsonValue, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(f"{context} must be an integer")
    return value


def _strings(value: JsonValue, context: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        _fail(f"{context} must be a string array")
    return cast("list[str]", value)


def _json_strings(value: list[str]) -> list[JsonValue]:
    result: list[JsonValue] = []
    result.extend(value)
    return result


def _fail(message: str) -> Never:
    raise IsolationError(message)


if __name__ == "__main__":
    raise SystemExit(main())
