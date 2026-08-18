"""Assemble closed candidate image contexts from immutable Git blobs."""

from __future__ import annotations

import asyncio
import hashlib
import re
import shutil
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Final, Never

import rfc8785

if TYPE_CHECKING:
    from ops.testing.isolation_common import JsonObject, JsonValue


SHA40: Final = re.compile(r"[0-9a-f]{40}")
TREE_FIELD_COUNT: Final = 3
GIT: Final = shutil.which("git")
APPLICATION_EXACT: Final = frozenset(
    {
        "Dockerfile",
        "manage.py",
        "ops/testing/timezone_contract.py",
        "pyproject.toml",
        "sitecustomize.py",
        "uv.lock",
    }
)
APPLICATION_PREFIXES: Final = (
    "apps/",
    "config/",
    "ops/container/",
    "static/",
    "templates/",
)
RUNNER_EXACT: Final = frozenset(
    {"config/runtime.py", "pyproject.toml", "sitecustomize.py", "uv.lock"}
)
RUNNER_PREFIXES: Final = ("ops/testing/",)
RUNNER_SUITE_PATHS: Final = {
    "availability": "ops/testing/browser_suites/availability.py",
    "patient": "ops/testing/browser_suites/patient.py",
    "runtime-https": "ops/testing/browser_suites/runtime_https.py",
    "scheduling": "ops/testing/browser_suites/scheduling.py",
}
DOCKERIGNORE = {
    "application": "ops/testing/application-image.dockerignore",
    "browser-runner": "ops/testing/browser-runner.dockerignore",
}
DOCKERFILE = {
    "application": "Dockerfile",
    "browser-runner": "ops/testing/browser-runner.Dockerfile",
}


def assemble_candidate_context(
    repository: Path,
    destination: Path,
    revision: str,
    kind: str,
) -> JsonObject:
    """Materialize one clean exact-revision context without worktree copies."""
    _require_clean_revision(repository, revision)
    if kind not in DOCKERIGNORE or destination.exists() or destination.is_symlink():
        _fail()
    sources = _tree_entries(repository, revision)
    targets = [
        target
        for _, _, source_path in sources
        if (target := _target_path(kind, source_path)) is not None
    ]
    if not {".dockerignore", "Dockerfile", "pyproject.toml", "uv.lock"} <= set(
        targets
    ) or len(targets) != len(set(targets)):
        _fail()
    destination.mkdir(mode=0o700)
    tree = _text_command(repository, "rev-parse", f"{revision}^{{tree}}")
    entries: list[JsonValue] = []
    for mode, object_id, source_path in sources:
        target_path = _target_path(kind, source_path)
        if target_path is None:
            continue
        raw = _command(repository, "cat-file", "blob", object_id)
        output_path = destination / target_path
        output_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        output_path.write_bytes(raw)
        output_path.chmod(0o755 if mode == "100755" else 0o644)
        entries.append(
            {
                "git_mode": mode,
                "object_id": object_id,
                "path": target_path,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "size_bytes": len(raw),
                "source_path": source_path,
            }
        )
    entries.sort(key=lambda item: str(item["path"]) if isinstance(item, dict) else "")
    manifest: JsonObject = {
        "entries": entries,
        "kind": kind,
        "revision_sha": revision,
        "schema_version": 1,
        "tree_sha": tree,
    }
    manifest_raw = rfc8785.dumps(manifest) + b"\n"
    manifest_name = f"{kind}-source-manifest.json"
    (destination / manifest_name).write_bytes(manifest_raw)
    return {
        "available_suite_ids": _json_strings(_available_suites(kind, sources)),
        "kind": kind,
        "revision_sha": revision,
        "source_entry_count": len(entries),
        "source_manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
        "tree_sha": tree,
    }


def _available_suites(kind: str, sources: list[tuple[str, str, str]]) -> list[str]:
    if kind == "application":
        return []
    paths = {path for _, _, path in sources}
    return sorted(
        suite_id
        for suite_id, source_path in RUNNER_SUITE_PATHS.items()
        if source_path in paths
    )


def _json_strings(value: list[str]) -> list[JsonValue]:
    result: list[JsonValue] = []
    result.extend(value)
    return result


def _require_clean_revision(repository: Path, revision: str) -> None:
    if (
        SHA40.fullmatch(revision) is None
        or _text_command(repository, "rev-parse", "HEAD") != revision
        or _command(repository, "status", "--porcelain=v1", "--untracked-files=all")
    ):
        _fail()


def _tree_entries(
    repository: Path,
    revision: str,
) -> list[tuple[str, str, str]]:
    raw = _command(repository, "ls-tree", "-rz", "--full-tree", revision)
    result: list[tuple[str, str, str]] = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        metadata, separator, encoded_path = record.partition(b"\t")
        fields = metadata.decode("ascii").split(" ")
        path = encoded_path.decode("utf-8")
        pure = PurePosixPath(path)
        if (
            not separator
            or len(fields) != TREE_FIELD_COUNT
            or fields[1] != "blob"
            or fields[0] not in {"100644", "100755"}
            or pure.is_absolute()
            or ".." in pure.parts
        ):
            _fail()
        result.append((fields[0], fields[2], path))
    return result


def _target_path(kind: str, source_path: str) -> str | None:
    if source_path == DOCKERIGNORE[kind]:
        return ".dockerignore"
    if source_path == DOCKERFILE[kind]:
        return "Dockerfile"
    exact = APPLICATION_EXACT if kind == "application" else RUNNER_EXACT
    prefixes = APPLICATION_PREFIXES if kind == "application" else RUNNER_PREFIXES
    if source_path in exact or source_path.startswith(prefixes):
        return source_path
    return None


def _text_command(repository: Path, *arguments: str) -> str:
    return _command(repository, *arguments).decode("ascii").strip()


def _command(repository: Path, *arguments: str) -> bytes:
    if GIT is None:
        _fail()
    return asyncio.run(_async_command(GIT, repository, arguments))


async def _async_command(
    executable: str,
    repository: Path,
    arguments: tuple[str, ...],
) -> bytes:
    process = await asyncio.create_subprocess_exec(
        executable,
        "-C",
        str(repository),
        *arguments,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await process.communicate()
    if process.returncode != 0:
        _fail()
    return stdout


def _fail() -> Never:
    message = "candidate source contract failed"
    raise _CandidateSourceError(message)


class _CandidateSourceError(RuntimeError):
    pass
