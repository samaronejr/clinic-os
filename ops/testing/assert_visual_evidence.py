"""Validate one published browser-evidence manifest and its artifact set."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path, PurePosixPath
from typing import Final, Never

MANIFEST_KEYS: Final = frozenset({"artifacts", "schema_version", "suite_ids"})
SUMMARY_KEYS: Final = frozenset(
    {
        "blocking_violation_count",
        "console_messages",
        "schema_version",
        "suite_id",
        "total_violation_count",
        "viewports",
        "violations",
    }
)
ALLOWED_SUFFIXES: Final = frozenset({".json", ".png"})
FORBIDDEN_SUFFIXES: Final = frozenset({".har", ".zip", ".webm", ".trace"})
REQUIRED_VIEWPORTS: Final = (
    "mobile-375",
    "tablet-768",
    "desktop-1280",
    "desktop-1280-zoom-200",
)
DIGEST_LENGTH: Final = 64
ARGUMENT_COUNT: Final = 2


class VisualEvidenceError(RuntimeError):
    """Reject an incomplete, unbounded, or leaking evidence publication."""

    def __init__(self, reason: str) -> None:
        """Expose one precise non-identifying validation failure."""
        super().__init__(f"visual evidence rejected: {reason}")


def _fail(reason: str) -> Never:
    raise VisualEvidenceError(reason)


def _load(path: Path) -> dict[str, object]:
    document: object = json.loads(path.read_bytes())
    if not isinstance(document, dict):
        _fail(f"{path.name} is not a JSON object")
    return {str(key): value for key, value in document.items()}


def _artifact_map(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        _fail("artifacts must be an object")
    result: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            _fail("artifact entries must be string pairs")
        if len(item) != DIGEST_LENGTH:
            _fail(f"artifact {key} has a malformed digest")
        result[key] = item
    return result


def _validate_relative_path(relative: str) -> None:
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts or pure.as_posix() != relative:
        _fail(f"artifact path {relative} is not a bounded relative path")
    if pure.suffix not in ALLOWED_SUFFIXES:
        _fail(f"artifact path {relative} has a forbidden suffix")


def validate_summary(summary: dict[str, object]) -> None:
    """Require a complete, zero-violation, clean-console suite summary."""
    if set(summary) != set(SUMMARY_KEYS):
        _fail("summary has the wrong closed key set")
    if summary.get("schema_version") != 1:
        _fail("summary schema version is not 1")
    if summary.get("blocking_violation_count") != 0:
        _fail("summary reports blocking accessibility violations")
    if summary.get("console_messages") != []:
        _fail("summary reports browser console output")
    if summary.get("viewports") != list(REQUIRED_VIEWPORTS):
        _fail("summary does not cover the required viewport matrix")


def _validate_digests(root: Path, artifacts: dict[str, str]) -> None:
    for relative, digest in sorted(artifacts.items()):
        _validate_relative_path(relative)
        published = root / relative
        if not published.is_file() or published.is_symlink():
            _fail(f"artifact {relative} is missing")
        if hashlib.sha256(published.read_bytes()).hexdigest() != digest:
            _fail(f"artifact {relative} digest drifted")


def _validate_summaries(
    root: Path,
    artifacts: dict[str, str],
    suites: list[object],
) -> None:
    for suite in suites:
        summary_key = f"browser/{suite}/summary.json"
        if summary_key not in artifacts:
            _fail(f"suite {suite} published no summary")
        validate_summary(_load(root / summary_key))


def _reject_raw_captures(root: Path) -> None:
    for existing in root.rglob("*"):
        if existing.is_file() and existing.suffix in FORBIDDEN_SUFFIXES:
            _fail(f"raw capture {existing.name} was retained")


def _manifest_suites(manifest: dict[str, object]) -> list[object]:
    if set(manifest) != set(MANIFEST_KEYS):
        _fail("manifest has the wrong closed key set")
    if manifest.get("schema_version") != 1:
        _fail("manifest schema version is not 1")
    suites = manifest.get("suite_ids")
    if not isinstance(suites, list) or not suites:
        _fail("manifest declares no suite")
    return suites


def validate_manifest(manifest_path: Path) -> dict[str, str]:
    """Validate the manifest, every digest, and the absence of raw captures."""
    if not manifest_path.is_file() or manifest_path.is_symlink():
        _fail("manifest is not a regular file")
    root = manifest_path.parent
    manifest = _load(manifest_path)
    suites = _manifest_suites(manifest)
    artifacts = _artifact_map(manifest.get("artifacts"))
    if not artifacts:
        _fail("manifest publishes no artifact")
    _validate_digests(root, artifacts)
    _validate_summaries(root, artifacts, suites)
    _reject_raw_captures(root)
    return artifacts


def main() -> int:
    """Validate the manifest named by the single command-line argument."""
    if len(sys.argv) != ARGUMENT_COUNT:
        sys.stderr.write("usage: assert_visual_evidence.py <manifest.json>\n")
        return 2
    try:
        artifacts = validate_manifest(Path(sys.argv[1]))
    except (VisualEvidenceError, OSError, ValueError) as error:
        sys.stderr.write(f"{error}\n")
        return 2
    sys.stdout.write(f"validated {len(artifacts)} artifacts\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
