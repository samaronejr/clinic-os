from __future__ import annotations

import hashlib
import re
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Final

import pytest


class ApprovedPlanArtifactError(RuntimeError):
    pass


PROJECT_ROOT: Final = Path(__file__).resolve().parents[1]
APPROVED_PLAN: Final = PROJECT_ROOT / "docs" / "plans" / "clinic-os-phase1a-approved.md"
APPROVED_PLAN_SIDECAR: Final = APPROVED_PLAN.with_suffix(".sha256")
APPROVED_PLAN_SHA256: Final = (
    "1cb6d1702dc8960b7bccced4c69a0193418d22d29a521498b083eb6c891dff19"
)
SIDECAR_PATTERN: Final = re.compile(rb"[0-9a-f]{64}\n")


def _git_executable() -> str:
    executable = shutil.which("git")
    if executable is None:
        message = "Git is required to authenticate tracked approved-plan inputs"
        raise ApprovedPlanArtifactError(message)
    return executable


GIT_EXECUTABLE: Final = _git_executable()


def _read_regular(path: Path) -> bytes:
    if not path.exists():
        message = f"approved-plan artifact is missing: {path.name}"
        raise ApprovedPlanArtifactError(message)
    if path.is_symlink() or not stat.S_ISREG(path.stat(follow_symlinks=False).st_mode):
        message = f"approved-plan artifact must be a regular non-symlink: {path.name}"
        raise ApprovedPlanArtifactError(message)
    return path.read_bytes()


def _validate_artifact(plan: Path, sidecar: Path, expected_sha256: str) -> None:
    raw_plan = _read_regular(plan)
    raw_sidecar = _read_regular(sidecar)
    if SIDECAR_PATTERN.fullmatch(raw_sidecar) is None:
        message = "approved-plan sidecar must be lowercase SHA-256 plus LF"
        raise ApprovedPlanArtifactError(message)
    expected_sidecar = f"{expected_sha256}\n".encode()
    if raw_sidecar != expected_sidecar:
        message = "approved-plan sidecar does not match the frozen source digest"
        raise ApprovedPlanArtifactError(message)
    if hashlib.sha256(raw_plan).hexdigest() != expected_sha256:
        message = "approved-plan bytes do not match the frozen source digest"
        raise ApprovedPlanArtifactError(message)


def _assert_git_tracked(path: Path) -> None:
    relative_path = path.relative_to(PROJECT_ROOT)
    result = subprocess.run(  # noqa: S603 - fixed read-only Git metadata query.
        (
            GIT_EXECUTABLE,
            "-C",
            str(PROJECT_ROOT),
            "ls-files",
            "--error-unmatch",
            "--",
            str(relative_path),
        ),
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        message = f"approved-plan artifact is not tracked: {relative_path}"
        raise ApprovedPlanArtifactError(message)


def _write_valid_fixture(root: Path) -> tuple[Path, Path, str]:
    plan = root / "clinic-os-phase1a-approved.md"
    sidecar = plan.with_suffix(".sha256")
    raw = b"# fixture approved plan\n"
    digest = hashlib.sha256(raw).hexdigest()
    plan.write_bytes(raw)
    sidecar.write_text(f"{digest}\n", encoding="ascii")
    return plan, sidecar, digest


def test_tracked_approved_plan_matches_the_frozen_source_digest() -> None:
    # Given: the sole hosted-CI plan paths and the invoked source's frozen digest.
    artifact_paths = (APPROVED_PLAN, APPROVED_PLAN_SIDECAR)

    # When: the committed artifact pair is authenticated without any draft source.
    _validate_artifact(*artifact_paths, APPROVED_PLAN_SHA256)

    # Then: both authenticated inputs are regular, non-symlink Git-tracked files.
    for artifact_path in artifact_paths:
        _assert_git_tracked(artifact_path)


def test_validator_accepts_no_draft_or_untracked_source_lookup(tmp_path: Path) -> None:
    # Given: a valid explicit pair beside misleading draft and authority candidates.
    plan, sidecar, digest = _write_valid_fixture(tmp_path)
    (tmp_path / "initial_plan_en.md").write_bytes(b"unapproved draft\n")
    authority_plan = tmp_path / ".omo" / "plans" / plan.name
    authority_plan.parent.mkdir(parents=True)
    authority_plan.write_bytes(b"untracked authority candidate\n")

    # When: validation receives only the explicit tracked-CI pair.
    _validate_artifact(plan, sidecar, digest)

    # Then: unrelated source candidates cannot influence authentication.


def test_validator_rejects_a_missing_plan(tmp_path: Path) -> None:
    # Given: only a syntactically valid sidecar exists.
    plan = tmp_path / "missing.md"
    digest = hashlib.sha256(b"").hexdigest()
    sidecar = plan.with_suffix(".sha256")
    sidecar.write_text(f"{digest}\n", encoding="ascii")

    # When / Then: validation fails closed on the absent tracked bytes.
    with pytest.raises(ApprovedPlanArtifactError, match="missing"):
        _validate_artifact(plan, sidecar, digest)


def test_validator_rejects_a_missing_sidecar(tmp_path: Path) -> None:
    # Given: approved bytes exist without their authenticated sidecar.
    plan = tmp_path / "approved.md"
    raw = b"approved\n"
    plan.write_bytes(raw)

    # When / Then: validation fails closed on the absent digest record.
    with pytest.raises(ApprovedPlanArtifactError, match="missing"):
        _validate_artifact(
            plan, plan.with_suffix(".sha256"), hashlib.sha256(raw).hexdigest()
        )


def test_validator_rejects_an_extra_plan_byte(tmp_path: Path) -> None:
    # Given: a valid pair whose plan gains one unapproved byte.
    plan, sidecar, digest = _write_valid_fixture(tmp_path)
    plan.write_bytes(plan.read_bytes() + b"\n")

    # When / Then: exact-byte authentication rejects the appended byte.
    with pytest.raises(ApprovedPlanArtifactError, match="bytes"):
        _validate_artifact(plan, sidecar, digest)


def test_validator_rejects_tampered_plan_bytes(tmp_path: Path) -> None:
    # Given: a valid pair whose plan content is replaced at equal length.
    plan, sidecar, digest = _write_valid_fixture(tmp_path)
    plan.write_bytes(b"# fixture rejected plan\n")

    # When / Then: digest authentication rejects the substituted content.
    with pytest.raises(ApprovedPlanArtifactError, match="bytes"):
        _validate_artifact(plan, sidecar, digest)


def test_validator_rejects_noncanonical_sidecar_grammar(tmp_path: Path) -> None:
    # Given: valid plan bytes with an uppercase digest and no terminal LF.
    plan, sidecar, digest = _write_valid_fixture(tmp_path)
    sidecar.write_text(digest.upper(), encoding="ascii")

    # When / Then: the sidecar grammar rejects non-lowercase or missing-LF bytes.
    with pytest.raises(ApprovedPlanArtifactError, match="lowercase"):
        _validate_artifact(plan, sidecar, digest)


@pytest.mark.parametrize("symlink_plan", [True, False], ids=["plan", "sidecar"])
def test_validator_rejects_symlinked_artifacts(
    tmp_path: Path,
    symlink_plan: bool,
) -> None:
    # Given: one artifact path is replaced by a symlink to regular bytes.
    plan, sidecar, digest = _write_valid_fixture(tmp_path)
    selected = plan if symlink_plan else sidecar
    target = selected.with_name(f"{selected.name}.target")
    selected.replace(target)
    selected.symlink_to(target)

    # When / Then: path-shape authentication rejects the symlink.
    with pytest.raises(ApprovedPlanArtifactError, match="non-symlink"):
        _validate_artifact(plan, sidecar, digest)
