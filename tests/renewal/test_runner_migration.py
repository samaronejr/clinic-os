from __future__ import annotations

import copy
import hashlib
import os
import stat
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
import rfc8785
from config.settings.browser_authority_root import validate_authority_root
from ops.testing import current_source_snapshot as current_source
from ops.testing import (
    https_image_controller,
    https_probes,
    https_stack_runtime,
)
from ops.testing.https_stack_specs import HttpsStackInput, build_https_stack_plan
from ops.testing.image_source import assemble_candidate_context
from ops.testing.isolation_common import (
    IsolationError,
    JsonObject,
    JsonValue,
    canonical_bytes,
    load_json,
    write_no_replace,
)
from ops.testing.isolation_tracked_snapshot import (
    TrackedSnapshotRequest,
    snapshot_tracked_ledger,
)
from ops.testing.tls_export import PublicCaExport
from ops.testing.tls_materializer import MaterializerLease
from ops.testing.tls_specs import materializer_volume_names

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from ops.testing.https_stack_specs import HttpsStackPlan

FOUNDATION_SHA = "a" * 40
PLAN_PATH = "docs/plans/clinic-os-phase1a-approved.md"
TRACKED_FILES = {
    ".gitignore": ".omo/\n__pycache__/\n.venv/\n",
    "Dockerfile": "FROM scratch\n",
    "apps/web/app.py": "VALUE = 1\n",
    "config/runtime.py": "RUNTIME = True\n",
    "docs/guide.md": "legacy documentation\n",
    "docs/plans/clinic-os-phase1a-approved.md": "# approved tracked plan\n",
    "manage.py": "print('fixture')\n",
    "ops/container/entrypoint.sh": '#!/bin/sh\nexec "$@"\n',
    "ops/testing/application-image.dockerignore": "**\n",
    "ops/testing/browser-runner.Dockerfile": "FROM scratch\n",
    "ops/testing/browser-runner.dockerignore": "**\n",
    "ops/testing/browser_suites/patient.py": "SUITE = 'patient'\n",
    "ops/testing/timezone_contract.py": "TZ = 1\n",
    "pyproject.toml": "[project]\nname='fixture'\n",
    "sitecustomize.py": "",
    "tests/test_fixture.py": "def test_fixture() -> None:\n    assert True\n",
    "uv.lock": "version = 1\n",
}


def _git(root: Path, *arguments: str) -> str:
    executable = current_source.GIT
    assert executable is not None
    result = subprocess.run(  # noqa: S603 - fixed executable and test-owned repository.
        (executable, "-C", str(root), *arguments),
        check=True,
        capture_output=True,
    )
    return result.stdout.decode().strip()


def _tracked_repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    plan_raw = TRACKED_FILES["docs/plans/clinic-os-phase1a-approved.md"].encode()
    digest = hashlib.sha256(plan_raw).hexdigest()
    files = {
        **TRACKED_FILES,
        "docs/plans/clinic-os-phase1a-approved.sha256": f"{digest}\n",
    }
    for relative_path, content in files.items():
        path = repository / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    (repository / "ops/container/entrypoint.sh").chmod(0o755)
    _git(repository, "init", "--quiet")
    _git(repository, "config", "user.name", "Clinic Test")
    _git(repository, "config", "user.email", "clinic@example.invalid")
    _git(repository, "add", "-A")
    _git(repository, "commit", "--quiet", "-m", "fixture")
    return repository


def _tracked_ledger(repository: Path) -> Path:
    evidence_root = repository / ".omo" / "evidence"
    evidence_root.parent.mkdir(mode=0o700)
    request = TrackedSnapshotRequest(
        approved_plan=repository / PLAN_PATH,
        tracked_ci_sidecar=repository / f"{PLAN_PATH.removesuffix('.md')}.sha256",
        foundation_sha=FOUNDATION_SHA,
        worktree=repository,
        evidence_root=evidence_root,
        inventory_fixture={
            "containers": [],
            "listeners": [],
            "networks": [],
            "volumes": [],
        },
    )
    return snapshot_tracked_ledger(request)


def _fixture(tmp_path: Path) -> tuple[Path, Path]:
    repository = _tracked_repository(tmp_path)
    ledger_path = _tracked_ledger(repository)
    assert ledger_path == repository / ".omo/evidence/isolation-ledger-phase1a.json"
    return repository, ledger_path


def _staging(tmp_path: Path, name: str = "staging") -> Path:
    staging = tmp_path / name
    staging.mkdir(mode=0o700)
    return staging


def _capture(
    repository: Path,
    staging: Path,
    authorized: tuple[str, ...] = (),
) -> tuple[JsonObject, str]:
    return current_source.capture_current_source(
        repository,
        staging,
        authorized_untracked=authorized,
    )


def _entry(manifest: JsonObject, path: str) -> JsonObject:
    for entry in _entries(manifest):
        if entry["path"] == path:
            return entry
    message = f"missing manifest entry: {path}"
    raise AssertionError(message)


def _entries(manifest: JsonObject) -> list[JsonObject]:
    entries = manifest["entries"]
    assert isinstance(entries, list)
    result: list[JsonObject] = []
    for entry in entries:
        assert isinstance(entry, dict)
        result.append(entry)
    return result


def _entry_paths(manifest: JsonObject) -> list[str]:
    paths: list[str] = []
    for entry in _entries(manifest):
        path = entry["path"]
        assert isinstance(path, str)
        paths.append(path)
    return paths


def _run_root(tmp_path: Path, name: str = "run-root") -> Path:
    root = tmp_path / name
    root.mkdir(mode=0o700)
    return root


def _fake_builder(image_id: str) -> Callable[[Path, JsonObject, Path], None]:
    def build(context: Path, contract: JsonObject, iidfile: Path) -> None:
        assert (context / "Dockerfile").is_file()
        assert contract["source_manifest_sha256"]
        iidfile.write_text(f"{image_id}\n", encoding="ascii")

    return build


def _fake_inspector(record: JsonObject) -> Callable[[str], JsonObject]:
    def inspect(image_id: str) -> JsonObject:
        images = record["images"]
        assert isinstance(images, dict)
        contexts = record["contexts"]
        assert isinstance(contexts, dict)
        for kind, candidate in images.items():
            if candidate != image_id:
                continue
            context = contexts[kind]
            assert isinstance(context, dict)
            contract = context["contract"]
            assert isinstance(contract, dict)
            labels = current_source.expected_image_labels(contract)
            result: JsonObject = {
                "Architecture": "amd64",
                "Config": {"Labels": cast("JsonValue", labels)},
                "Id": image_id,
            }
            return result
        message = f"unknown image {image_id}"
        raise AssertionError(message)

    return inspect


def _build_record(
    repository: Path,
    run_root: Path,
    report: Path,
    authorized: tuple[str, ...] = (),
    kinds: tuple[str, ...] = ("application",),
) -> JsonObject:
    image_id = "sha256:" + "b" * 64
    return current_source.build_current_source_record(
        repository,
        run_root,
        report,
        authorized_untracked=authorized,
        kinds=kinds,
        builders={"application": _fake_builder(image_id)},
    )


def test_snapshot_captures_modified_tracked_and_authorized_untracked_bytes(
    tmp_path: Path,
) -> None:
    # Given: a committed fixture repository with an authenticated tracked ledger.
    repository, _ = _fixture(tmp_path)
    (repository / "apps/web/app.py").write_text("VALUE = 2\n")
    (repository / "docs/guide.md").unlink()
    (repository / "ops/testing/local_helper.py").write_text("HELPER = 1\n")
    (repository / "stray-notes.txt").write_text("unauthorized\n")

    # When: the current-source snapshot runs with one authorized untracked path.
    staging = _staging(tmp_path)
    manifest, digest = _capture(repository, staging, ("ops/testing/local_helper.py",))

    # Then: current bytes, the deletion, and only the authorized addition appear.
    modified = _entry(manifest, "apps/web/app.py")
    assert modified["sha256"] == hashlib.sha256(b"VALUE = 2\n").hexdigest()
    assert modified["source"] == "tracked"
    added = _entry(manifest, "ops/testing/local_helper.py")
    assert added["source"] == "untracked"
    assert added["sha256"] == hashlib.sha256(b"HELPER = 1\n").hexdigest()
    assert manifest["deleted_tracked_paths"] == ["docs/guide.md"]
    assert manifest["authorized_untracked_paths"] == ["ops/testing/local_helper.py"]
    assert manifest["unauthorized_untracked_count"] == 1
    paths = _entry_paths(manifest)
    assert "stray-notes.txt" not in paths
    assert "docs/guide.md" not in paths
    assert paths == sorted(paths)
    assert manifest["base_revision_sha"] == _git(repository, "rev-parse", "HEAD")
    assert manifest["base_tree_sha"] == _git(repository, "rev-parse", "HEAD^{tree}")
    manifest_file = staging / "current-source-manifest.json"
    assert manifest_file.read_bytes() == rfc8785.dumps(manifest) + b"\n"
    assert digest == hashlib.sha256(manifest_file.read_bytes()).hexdigest()
    payload = staging / "payload" / "apps/web/app.py"
    assert payload.read_bytes() == b"VALUE = 2\n"


def test_second_snapshot_after_fixture_change_produces_a_different_digest(
    tmp_path: Path,
) -> None:
    # Given: one captured snapshot of the fixture repository.
    repository, _ = _fixture(tmp_path)
    first_manifest, first_digest = _capture(repository, _staging(tmp_path, "one"))

    # When: one authorized fixture source file changes and a second snapshot runs.
    suite = repository / "ops/testing/browser_suites/patient.py"
    suite.write_text("SUITE = 'patient-v2'\n")
    second_manifest, second_digest = _capture(repository, _staging(tmp_path, "two"))

    # Then: the digest differs and names exactly the changed entry.
    assert first_digest != second_digest
    changed = _entry(second_manifest, "ops/testing/browser_suites/patient.py")
    original = _entry(first_manifest, "ops/testing/browser_suites/patient.py")
    assert changed["sha256"] != original["sha256"]
    assert changed["sha256"] == hashlib.sha256(b"SUITE = 'patient-v2'\n").hexdigest()
    unchanged = _entry(second_manifest, "apps/web/app.py")
    assert unchanged == _entry(first_manifest, "apps/web/app.py")


def test_snapshot_rejects_a_conflicted_index(tmp_path: Path) -> None:
    # Given: an unmerged index entry injected through the real index plumbing.
    repository, _ = _fixture(tmp_path)
    subprocess.run(  # noqa: S603 - fixed executable and test-owned repository.
        (
            current_source.GIT or "git",
            "-C",
            str(repository),
            "update-index",
            "--index-info",
        ),
        input=(
            f"100644 {'1' * 40} 1\tapps/web/app.py\n"
            f"100644 {'2' * 40} 2\tapps/web/app.py\n"
            f"100644 {'3' * 40} 3\tapps/web/app.py\n"
        ).encode(),
        check=True,
        capture_output=True,
    )

    # When / Then: the conflicted index is rejected before any capture.
    with pytest.raises(IsolationError, match="conflict"):
        _capture(repository, _staging(tmp_path))


@pytest.mark.parametrize(
    "case",
    [
        "tracked-symlink",
        "untracked-symlink",
        "untracked-fifo",
        "escape",
        "absolute",
        "tracked-collision",
        "excluded-env",
    ],
)
def test_snapshot_rejects_malformed_inputs(tmp_path: Path, case: str) -> None:
    # Given: one malformed candidate for the current-source snapshot.
    repository, _ = _fixture(tmp_path)
    authorized: tuple[str, ...] = ()
    if case == "tracked-symlink":
        (repository / "apps/web/link.py").symlink_to("app.py")
        _git(repository, "add", "apps/web/link.py")
    elif case == "untracked-symlink":
        (repository / "linked.py").symlink_to("apps/web/app.py")
        authorized = ("linked.py",)
    elif case == "untracked-fifo":
        os.mkfifo(repository / "pipe")
        authorized = ("pipe",)
    elif case == "escape":
        authorized = ("../outside.py",)
    elif case == "absolute":
        authorized = (str(repository / "apps/web/app.py"),)
    elif case == "tracked-collision":
        authorized = ("apps/web/app.py",)
    else:
        (repository / ".env").write_text("SECRET=1\n")
        authorized = (".env",)

    # When / Then: the malformed input is rejected without a manifest.
    staging = _staging(tmp_path)
    with pytest.raises(IsolationError):
        _capture(repository, staging, authorized)
    assert not (staging / "current-source-manifest.json").exists()


@pytest.mark.parametrize("target", ["outside", "inside"])
def test_snapshot_rejects_a_symlinked_ancestor(tmp_path: Path, target: str) -> None:
    # Given: a tracked file whose parent directory is replaced by a symlink.
    repository, _ = _fixture(tmp_path)
    if target == "outside":
        relocated = tmp_path / "outside-web"
    else:
        relocated = repository / "docs" / "inside-web"
    (repository / "apps/web").rename(relocated)
    (repository / "apps/web").symlink_to(relocated)
    (relocated / "app.py").write_text("OUTSIDE_REPOSITORY = True\n")

    # When / Then: capture refuses the ancestor traversal and writes nothing.
    staging = _staging(tmp_path)
    with pytest.raises(IsolationError):
        _capture(repository, staging)
    assert not (staging / "current-source-manifest.json").exists()


@pytest.mark.parametrize(
    "path",
    [
        ".claude/settings.json",
        ".codex/config.toml",
        ".agents/skills/local/SKILL.md",
        ".pytest_cache/v/cache/nodeids",
        ".mypy_cache/3.12/cache.db",
        "nested/.pytest_cache/v/cache/nodeids",
    ],
)
def test_snapshot_rejects_allowlisted_excluded_components(
    tmp_path: Path, path: str
) -> None:
    # Given: an excluded-class file explicitly authorized as untracked input.
    repository, _ = _fixture(tmp_path)
    candidate = repository / path
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_text("PRIVATE = 'marker'\n")

    # When / Then: the allowlist cannot override the exclusion policy.
    staging = _staging(tmp_path)
    with pytest.raises(IsolationError, match="excluded"):
        _capture(repository, staging, (path,))
    assert not (staging / "current-source-manifest.json").exists()


def test_snapshot_skips_tracked_files_under_excluded_components(
    tmp_path: Path,
) -> None:
    # Given: a tracked file beneath an agent-state component.
    repository, _ = _fixture(tmp_path)
    tracked = repository / ".claude" / "tracked.txt"
    tracked.parent.mkdir()
    tracked.write_text("TRACKED = True\n")
    _git(repository, "add", ".claude/tracked.txt")

    # When: the snapshot captures the repository.
    manifest, _ = _capture(repository, _staging(tmp_path))

    # Then: the excluded component is skipped like every other exclusion.
    assert ".claude/tracked.txt" not in _entry_paths(manifest)


def test_materialized_contexts_match_the_legacy_projection(tmp_path: Path) -> None:
    # Given: a captured snapshot containing files inside and outside each payload.
    repository, _ = _fixture(tmp_path)
    staging = _staging(tmp_path)
    manifest, _ = _capture(repository, staging)

    # When: both candidate contexts are materialized from the staged payload.
    application = current_source.materialize_current_context(
        staging, manifest, "application", tmp_path / "app-context"
    )
    runner = current_source.materialize_current_context(
        staging, manifest, "browser-runner", tmp_path / "runner-context"
    )

    # Then: each context carries exactly the legacy projection and manifest.
    app_files = sorted(
        str(path.relative_to(tmp_path / "app-context"))
        for path in (tmp_path / "app-context").rglob("*")
        if path.is_file()
    )
    assert app_files == [
        ".dockerignore",
        "Dockerfile",
        "application-source-manifest.json",
        "apps/web/app.py",
        "config/runtime.py",
        "manage.py",
        "ops/container/entrypoint.sh",
        "ops/testing/timezone_contract.py",
        "pyproject.toml",
        "sitecustomize.py",
        "uv.lock",
    ]
    assert "tests/test_fixture.py" not in app_files
    assert "docs/guide.md" not in app_files
    entrypoint = tmp_path / "app-context/ops/container/entrypoint.sh"
    assert stat.S_IMODE(entrypoint.stat().st_mode) == 0o755
    assert stat.S_IMODE((tmp_path / "app-context/manage.py").stat().st_mode) == 0o644
    contexts = manifest["contexts"]
    assert isinstance(contexts, dict)
    app_context = contexts["application"]
    assert isinstance(app_context, dict)
    assert application["kind"] == "application"
    assert application["available_suite_ids"] == []
    assert application["source_manifest_sha256"] == app_context["manifest_sha256"]
    runner_files = sorted(
        str(path.relative_to(tmp_path / "runner-context"))
        for path in (tmp_path / "runner-context").rglob("*")
        if path.is_file()
    )
    assert "ops/testing/browser_suites/patient.py" in runner_files
    assert "Dockerfile" in runner_files
    assert "apps/web/app.py" not in runner_files
    assert runner["kind"] == "browser-runner"
    assert runner["available_suite_ids"] == ["patient"]


def test_build_record_publishes_after_teardown_and_binds_images(
    tmp_path: Path,
) -> None:
    # Given: a fixture repository and a private run root.
    repository, _ = _fixture(tmp_path)
    run_root = _run_root(tmp_path)
    report = tmp_path / "report.json"

    # When: the record builds one candidate image through the injected builder.
    record = _build_record(repository, run_root, report)

    # Then: staging is gone before publication and the report is immutable.
    assert list(run_root.iterdir()) == []
    assert stat.S_IMODE(report.stat().st_mode) == 0o400
    loaded, raw = load_json(report)
    assert raw == canonical_bytes(loaded)
    assert loaded == record
    assert record["schema_version"] == 1
    images = record["images"]
    assert images == {"application": "sha256:" + "b" * 64}
    contexts = record["contexts"]
    assert isinstance(contexts, dict)
    application_context = contexts["application"]
    assert isinstance(application_context, dict)
    contract = application_context["contract"]
    assert isinstance(contract, dict)
    assert set(contract) == {
        "available_suite_ids",
        "kind",
        "revision_sha",
        "source_entry_count",
        "source_manifest_sha256",
        "tree_sha",
    }
    manifest = record["snapshot_manifest"]
    assert isinstance(manifest, dict)
    assert (
        record["snapshot_manifest_sha256"]
        == hashlib.sha256(rfc8785.dumps(manifest) + b"\n").hexdigest()
    )


def test_verify_accepts_a_fresh_record(tmp_path: Path) -> None:
    # Given: a published record for the untouched fixture repository.
    repository, _ = _fixture(tmp_path)
    report = tmp_path / "report.json"
    record = _build_record(repository, _run_root(tmp_path), report)

    # When / Then: verification re-authenticates and returns the record.
    verified = current_source.verify_current_source_record(
        repository,
        report,
        _run_root(tmp_path, "verify-root"),
        image_inspector=_fake_inspector(record),
    )
    assert verified["snapshot_manifest_sha256"] == record["snapshot_manifest_sha256"]


def test_verify_rejects_stale_source_after_a_tracked_change(
    tmp_path: Path,
) -> None:
    # Given: a published record, then a tracked source modification.
    repository, _ = _fixture(tmp_path)
    report = tmp_path / "report.json"
    record = _build_record(repository, _run_root(tmp_path), report)
    (repository / "apps/web/app.py").write_text("VALUE = 3\n")

    # When / Then: the stale manifest digest is rejected.
    with pytest.raises(IsolationError, match=r"stale|drift|differ"):
        current_source.verify_current_source_record(
            repository,
            report,
            _run_root(tmp_path, "verify-root"),
            image_inspector=_fake_inspector(record),
        )


def test_verify_rejects_mismatched_repository_ledger_and_plan(
    tmp_path: Path,
) -> None:
    # Given: a published record whose authority fields are then drifted.
    repository, _ = _fixture(tmp_path)
    report = tmp_path / "report.json"
    record = _build_record(repository, _run_root(tmp_path), report)

    for field, value in (
        ("repository", str(tmp_path / "other")),
        ("ledger_sha256", "0" * 64),
        ("approved_plan_sha256", "0" * 64),
    ):
        drifted = copy.deepcopy(record)
        drifted[field] = value
        drifted_path = tmp_path / f"drifted-{field}.json"
        write_no_replace(drifted_path, canonical_bytes(drifted), mode=0o400)
        with pytest.raises(IsolationError):
            current_source.verify_current_source_record(
                repository,
                drifted_path,
                _run_root(tmp_path, f"verify-{field}"),
                image_inspector=_fake_inspector(record),
            )

    # And: a modified frozen plan pair is rejected by re-authentication.
    plan = repository / PLAN_PATH
    plan.write_text("# uncommitted replacement\n")
    with pytest.raises(IsolationError):
        current_source.verify_current_source_record(
            repository,
            report,
            _run_root(tmp_path, "verify-plan"),
            image_inspector=_fake_inspector(record),
        )


def test_verify_rejects_a_stale_image(tmp_path: Path) -> None:
    # Given: a published record whose image no longer matches inspection.
    repository, _ = _fixture(tmp_path)
    report = tmp_path / "report.json"
    _build_record(repository, _run_root(tmp_path), report)

    def stale_inspector(image_id: str) -> JsonObject:
        return {
            "Architecture": "amd64",
            "Config": {"Labels": {}},
            "Id": "sha256:" + "9" * 64,
        }

    # When / Then: the drifted image identity is rejected.
    with pytest.raises(IsolationError, match="image"):
        current_source.verify_current_source_record(
            repository,
            report,
            _run_root(tmp_path, "verify-root"),
            image_inspector=stale_inspector,
        )


def test_interrupted_build_cleans_staging_and_publishes_nothing(
    tmp_path: Path,
) -> None:
    # Given: a builder that fails after the snapshot was captured.
    repository, _ = _fixture(tmp_path)
    run_root = _run_root(tmp_path)
    report = tmp_path / "report.json"

    def failing_builder(context: Path, contract: JsonObject, iidfile: Path) -> None:
        message = "synthetic build interruption"
        raise RuntimeError(message)

    # When / Then: the failure propagates, staging is removed, no report exists.
    with pytest.raises(RuntimeError, match="interruption"):
        current_source.build_current_source_record(
            repository,
            run_root,
            report,
            kinds=("application",),
            builders={"application": failing_builder},
        )
    assert list(run_root.iterdir()) == []
    assert not report.exists()


def test_source_replacement_during_capture_is_rejected(tmp_path: Path) -> None:
    # Given: a tracked file replaced underneath the snapshot read.
    repository, _ = _fixture(tmp_path)
    target = repository / "apps/web/app.py"
    original_read = os.read
    target_identity = target.stat()
    replaced = False

    def swapped(descriptor: int, count: int) -> bytes:
        nonlocal replaced
        data = original_read(descriptor, count)
        identity = os.fstat(descriptor)
        if not replaced and (
            identity.st_dev,
            identity.st_ino,
        ) == (target_identity.st_dev, target_identity.st_ino):
            target.write_text("VALUE = 'replaced-during-read'\n" * 40)
            replaced = True
        return data

    # When / Then: the identity change is detected and rejected.
    with pytest.MonkeyPatch().context() as monkeypatch:
        monkeypatch.setattr(os, "read", swapped)
        with pytest.raises(IsolationError):
            _capture(repository, _staging(tmp_path))


def test_payload_replacement_before_materialize_is_rejected(tmp_path: Path) -> None:
    # Given: a captured snapshot whose staged payload is then replaced.
    repository, _ = _fixture(tmp_path)
    staging = _staging(tmp_path)
    manifest, _ = _capture(repository, staging)
    payload = staging / "payload" / "apps/web/app.py"
    payload.write_text("VALUE = 'payload-replaced'\n")

    # When / Then: materialization rejects the digest mismatch.
    with pytest.raises(IsolationError):
        current_source.materialize_current_context(
            staging, manifest, "application", tmp_path / "context"
        )
    assert not (tmp_path / "context").exists()


def test_legacy_clean_revision_gate_is_unchanged(tmp_path: Path) -> None:
    # Given: the same fixture repository, first clean then dirty.
    repository, _ = _fixture(tmp_path)
    revision = _git(repository, "rev-parse", "HEAD")

    # When / Then: the legacy gate still rejects any worktree drift.
    (repository / "apps/web/app.py").write_text("VALUE = 9\n")
    with pytest.raises(RuntimeError, match="candidate source contract failed"):
        assemble_candidate_context(
            repository, tmp_path / "dirty", revision, "application"
        )
    assert not (tmp_path / "dirty").exists()


def test_https_controller_dispatches_legacy_and_current_source_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: both controller routes with inert downstream boundaries.
    repository = _tracked_repository(tmp_path)
    monkeypatch.chdir(repository)
    seen: list[tuple[str, JsonObject, Callable[[Path], None] | None]] = []

    def run_phases(
        repo: Path,
        image_id: str,
        contract: JsonObject,
        browser_probe: Callable[[Path], None] | None = None,
    ) -> None:
        assert repo == repository
        seen.append((image_id, contract, browser_probe))

    monkeypatch.setattr(https_image_controller, "_run_stack_phases", run_phases)
    monkeypatch.setattr(
        https_image_controller, "_clean_revision", lambda _repo: "c" * 40
    )
    envelope: JsonObject = {
        "image_contract": {"kind": "application"},
        "image_id": "sha256:" + "d" * 64,
    }
    monkeypatch.setattr(
        https_image_controller,
        "_candidate_envelope",
        lambda _ledger, _revision: envelope,
    )
    monkeypatch.setattr(
        https_image_controller,
        "load_json",
        lambda _path: ({"attempt_root": str(tmp_path)}, b"{}\n"),
    )

    monkeypatch.setattr(sys, "argv", ["controller", "smoke"])
    https_image_controller.main()
    assert seen == [("sha256:" + "d" * 64, {"kind": "application"}, None)]

    runner_image = "sha256:" + "f" * 64
    runner_contract: JsonObject = {"kind": "browser-runner"}
    record: JsonObject = {
        "contexts": {
            "application": {"contract": {"kind": "application"}},
            "browser-runner": {"contract": runner_contract},
        },
        "images": {
            "application": "sha256:" + "e" * 64,
            "browser-runner": runner_image,
        },
    }
    monkeypatch.setattr(
        https_image_controller,
        "verify_current_source_record",
        lambda _repo, _record, _root: record,
    )
    probe_calls: list[tuple[Path, str, JsonObject]] = []
    monkeypatch.setattr(
        https_image_controller,
        "probe_runner_image",
        lambda repo, image, contract: probe_calls.append((repo, image, contract)),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "controller",
            "smoke-current-source",
            "--record",
            str(tmp_path / "record.json"),
            "--run-root",
            str(tmp_path / "runs"),
        ],
    )
    https_image_controller.main()
    image_id, contract, probe = seen[-1]
    assert (image_id, contract) == ("sha256:" + "e" * 64, {"kind": "application"})
    assert probe is not None
    probe(repository)
    assert probe_calls == [(repository, runner_image, runner_contract)]

    for argv in (
        ["controller"],
        ["controller", "unknown"],
        ["controller", "smoke", "extra"],
        ["controller", "smoke-current-source", "--record", "only"],
    ):
        monkeypatch.setattr(sys, "argv", argv)
        with pytest.raises(IsolationError, match="invalid HTTPS image"):
            https_image_controller.main()


def _https_plan() -> HttpsStackPlan:
    contract: JsonObject = {
        "available_suite_ids": [],
        "kind": "application",
        "revision_sha": "3" * 40,
        "source_entry_count": 1,
        "source_manifest_sha256": "4" * 64,
        "tree_sha": "5" * 40,
    }
    materializer_id = "22222222-2222-4222-8222-222222222222"
    return build_https_stack_plan(
        HttpsStackInput(
            "11111111-1111-4111-8111-111111111111",
            materializer_id,
            materializer_volume_names("clinic_tls", materializer_id),
            "clinic_https_fixture",
            f"sha256:{'6' * 64}",
            f"sha256:{'7' * 64}",
            contract,
            "33333333-3333-4333-8333-333333333333",
            "source",
        )
    )


def test_https_stack_threads_browser_probe_and_reverse_cleans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: a real stack plan with every external boundary made inert.
    plan = _https_plan()
    ledger = tmp_path / ".omo/evidence/isolation-ledger-phase1a.json"
    ledger.parent.mkdir(parents=True)
    _ = ledger.write_bytes(b"{}\n")
    events: list[str] = []
    monkeypatch.setattr(https_stack_runtime, "_reserve", lambda _l, _s: None)
    monkeypatch.setattr(https_stack_runtime, "_create_owned_resources", lambda _p: None)
    created = iter(["cleartext-id", "database-id", "release-id", "web-id"])
    monkeypatch.setattr(
        https_stack_runtime,
        "_create_service",
        lambda _p, _service: next(created),
    )

    def docker(arguments: tuple[str, ...]) -> str:
        events.append(f"docker:{arguments[0]}")
        return ""

    monkeypatch.setattr(https_stack_runtime, "run_docker_command", docker)
    monkeypatch.setattr(
        https_stack_runtime, "_prove_readiness_outage", lambda _p, _c: None
    )
    monkeypatch.setattr(https_stack_runtime, "_wait_for_database", lambda _c: None)
    monkeypatch.setattr(
        https_stack_runtime,
        "reconcile_same_boot",
        lambda _l: events.append("reconcile"),
    )
    monkeypatch.setattr(
        https_stack_runtime,
        "verify_claim",
        lambda _l, _c, *, refresh: events.append("verify"),
    )
    monkeypatch.setattr(https_stack_runtime, "_prove_sigterm", lambda _c: None)
    removed: list[str] = []
    monkeypatch.setattr(https_stack_runtime, "_remove_container", removed.append)
    monkeypatch.setattr(
        https_stack_runtime,
        "_remove_network",
        lambda name: events.append(f"network:{name}"),
    )
    monkeypatch.setattr(
        https_stack_runtime,
        "_remove_volume",
        lambda name: events.append(f"volume:{name}"),
    )
    seen: list[Path] = []

    def probes(
        repository: Path,
        _plan: HttpsStackPlan,
        containers: dict[str, str],
        ca_path: Path,
        browser_probe: Callable[[Path], None] | None = None,
    ) -> None:
        assert set(containers) == {"cleartext", "database", "release", "web"}
        assert ca_path == tmp_path / "ca.pem"
        assert browser_probe is not None
        browser_probe(repository)
        events.append("probes")

    monkeypatch.setattr(https_stack_runtime, "run_https_probes", probes)

    # When: the stack runs with the current-source probe injected.
    https_stack_runtime.run_https_stack(
        tmp_path,
        plan,
        tmp_path / "ca.pem",
        browser_probe=seen.append,
    )

    # Then: the probe reached the probe stage and cleanup ran in reverse order.
    assert seen == [tmp_path]
    assert "probes" in events
    assert removed == ["web-id", "release-id", "database-id", "cleartext-id"]
    assert events[-3:] == [
        f"network:{plan.network_name}",
        f"volume:{plan.pgdata_name}",
        "reconcile",
    ]


def test_https_probes_dispatch_the_injected_or_legacy_browser_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: every probe boundary inert except the browser dispatch seam.
    plan = _https_plan()
    containers = {"database": "db", "release": "rel", "web": "web"}
    monkeypatch.setattr(https_probes, "inspect_https_runtime", lambda _p, _c: None)
    monkeypatch.setattr(https_probes, "configure_roles", lambda _c, _p: None)
    monkeypatch.setattr(https_probes, "run_docker_command", lambda _a: "")
    monkeypatch.setattr(https_probes, "_probe_database_tls", lambda _c, _p: None)
    monkeypatch.setattr(https_probes, "_probe_https", lambda _p, _c: None)
    calls: list[str] = []
    monkeypatch.setattr(
        https_probes,
        "_probe_browser_fixture",
        lambda repository: calls.append(f"legacy:{repository.name}"),
    )

    # When: no probe is injected, the legacy browser session runs unchanged.
    https_probes.run_https_probes(tmp_path, plan, containers, tmp_path / "ca.pem")
    # And: an injected probe replaces it at the seam.
    https_probes.run_https_probes(
        tmp_path,
        plan,
        containers,
        tmp_path / "ca.pem",
        browser_probe=lambda repository: calls.append(f"injected:{repository.name}"),
    )

    # Then: exactly one legacy call and one injected call, in order.
    assert calls == [f"legacy:{tmp_path.name}", f"injected:{tmp_path.name}"]


def _inert_https_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[list[tuple[Path, str, JsonObject]], list[Path]]:
    """Make every external resource boundary of the HTTPS route inert.

    The dispatch chain main -> _run_stack_phases -> run_https_stack ->
    run_https_probes stays real; only ledger mutation, Docker, curl, and the
    materializer/CA leases are intercepted.
    """
    ca_path = tmp_path / "ca.pem"
    ca_path.write_bytes(b"fixture-ca\n")
    materializer_id = "22222222-2222-4222-8222-222222222222"
    lease = MaterializerLease(
        materializer_id,
        "materializer-container",
        f"sha256:{'6' * 64}",
        tmp_path / ".omo/evidence/isolation-ledger-phase1a.json",
        "clinic_tls_fixture",
        materializer_volume_names("clinic_tls_fixture", materializer_id),
    )

    @contextmanager
    def lease_context(_repository: Path) -> Iterator[MaterializerLease]:
        yield lease

    @contextmanager
    def ca_context(_lease: MaterializerLease) -> Iterator[PublicCaExport]:
        yield PublicCaExport(
            "33333333-3333-4333-8333-333333333333",
            ca_path,
            hashlib.sha256(ca_path.read_bytes()).hexdigest(),
        )

    monkeypatch.setattr(https_image_controller, "materializer_lease", lease_context)
    monkeypatch.setattr(https_image_controller, "public_ca_export", ca_context)
    monkeypatch.setattr(https_stack_runtime, "reserve_claim", lambda _l, _s: "")
    monkeypatch.setattr(https_stack_runtime, "_create_owned_resources", lambda _p: None)
    created = iter(
        f"{phase}-{name}-id"
        for phase in ("source", "restore")
        for name in ("cleartext", "database", "release", "web")
    )
    monkeypatch.setattr(
        https_stack_runtime,
        "_create_service",
        lambda _p, _service: next(created),
    )
    monkeypatch.setattr(https_stack_runtime, "run_docker_command", lambda _a: "")
    monkeypatch.setattr(
        https_stack_runtime, "_prove_readiness_outage", lambda _p, _c: None
    )
    monkeypatch.setattr(https_stack_runtime, "_wait_for_database", lambda _c: None)
    monkeypatch.setattr(https_stack_runtime, "reconcile_same_boot", lambda _l: None)
    monkeypatch.setattr(
        https_stack_runtime, "verify_claim", lambda _l, _c, *, refresh: None
    )
    monkeypatch.setattr(https_stack_runtime, "_prove_sigterm", lambda _c: None)
    monkeypatch.setattr(https_stack_runtime, "_remove_container", lambda _c: None)
    monkeypatch.setattr(https_stack_runtime, "_remove_network", lambda _n: None)
    monkeypatch.setattr(https_stack_runtime, "_remove_volume", lambda _n: None)
    monkeypatch.setattr(https_probes, "inspect_https_runtime", lambda _p, _c: None)
    monkeypatch.setattr(https_probes, "configure_roles", lambda _c, _p: None)
    monkeypatch.setattr(https_probes, "run_docker_command", lambda _a: "")
    monkeypatch.setattr(https_probes, "_probe_database_tls", lambda _c, _p: None)
    monkeypatch.setattr(https_probes, "_probe_https", lambda _p, _c: None)
    probe_calls: list[tuple[Path, str, JsonObject]] = []
    monkeypatch.setattr(
        https_image_controller,
        "probe_runner_image",
        lambda repo, image, contract: probe_calls.append((repo, image, contract)),
    )
    legacy_calls: list[Path] = []
    monkeypatch.setattr(https_probes, "_probe_browser_fixture", legacy_calls.append)
    return probe_calls, legacy_calls


def test_current_source_route_delivers_the_record_browser_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: a real current-source record and every external boundary inert.
    repository, _ledger = _fixture(tmp_path)
    monkeypatch.chdir(repository)
    run_root = _run_root(tmp_path)
    report = tmp_path / "record.json"
    runner_image = "sha256:" + "f" * 64
    record = current_source.build_current_source_record(
        repository,
        run_root,
        report,
        kinds=("application", "browser-runner"),
        builders={
            "application": _fake_builder("sha256:" + "e" * 64),
            "browser-runner": _fake_builder(runner_image),
        },
    )
    real_verify = current_source.verify_current_source_record
    monkeypatch.setattr(
        https_image_controller,
        "verify_current_source_record",
        lambda repo, record_path, root: real_verify(
            repo, record_path, root, image_inspector=_fake_inspector(record)
        ),
    )
    probe_calls, legacy_calls = _inert_https_boundaries(tmp_path, monkeypatch)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "controller",
            "smoke-current-source",
            "--record",
            str(report),
            "--run-root",
            str(run_root),
        ],
    )

    # When: the real main -> phases -> stack -> probes chain executes.
    https_image_controller.main()

    # Then: both phases delivered the record-bound runner adapter, never the
    # legacy browser fixture.
    contexts = record["contexts"]
    assert isinstance(contexts, dict)
    runner_context = contexts["browser-runner"]
    assert isinstance(runner_context, dict)
    runner_contract = runner_context["contract"]
    assert probe_calls == [
        (repository, runner_image, runner_contract),
        (repository, runner_image, runner_contract),
    ]
    assert legacy_calls == []


def test_legacy_route_keeps_the_default_browser_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: the legacy smoke route with every external boundary inert.
    repository, _ledger = _fixture(tmp_path)
    monkeypatch.chdir(repository)
    probe_calls, legacy_calls = _inert_https_boundaries(tmp_path, monkeypatch)
    monkeypatch.setattr(
        https_image_controller, "_clean_revision", lambda _repo: "c" * 40
    )
    envelope: JsonObject = {
        "image_contract": {
            "available_suite_ids": [],
            "kind": "application",
            "revision_sha": "3" * 40,
            "source_entry_count": 1,
            "source_manifest_sha256": "4" * 64,
            "tree_sha": "5" * 40,
        },
        "image_id": "sha256:" + "d" * 64,
    }
    monkeypatch.setattr(
        https_image_controller,
        "_candidate_envelope",
        lambda _ledger, _revision: envelope,
    )
    monkeypatch.setattr(
        https_image_controller,
        "load_json",
        lambda _path: ({"attempt_root": str(tmp_path)}, b"{}\n"),
    )
    monkeypatch.setattr(sys, "argv", ["controller", "smoke"])

    # When: the real main -> phases -> stack -> probes chain executes.
    https_image_controller.main()

    # Then: both phases used the legacy browser fixture, never the adapter.
    assert legacy_calls == [repository, repository]
    assert probe_calls == []


def _authority_expected(ledger: JsonObject, repository: Path) -> JsonObject:
    return {
        "attempt_id": ledger["attempt_id"],
        "ca_export_claim_id": "11111111-1111-4111-8111-111111111111",
        "database_claim_id": "22222222-2222-4222-8222-222222222222",
        "database_name": "fixture_clinic",
        "database_port": 15432,
        "environment_contract": {},
        "foundation_sha": ledger["foundation_sha"],
        "materializer_claim_id": "33333333-3333-4333-8333-333333333333",
        "process_claim_id": "44444444-4444-4444-8444-444444444444",
        "process_port": 18443,
        "project": "clinic_phase1a_fixture",
        "worktree_realpath": str(repository),
    }


def test_authority_root_accepts_the_tracked_plan_layout_inside_the_worktree(
    tmp_path: Path,
) -> None:
    # Given: a tracked-CI ledger whose attempt root lives under worktree .omo.
    repository, ledger_path = _fixture(tmp_path)
    ledger, _ = load_json(ledger_path)
    expected = _authority_expected(ledger, repository)

    # When / Then: the exact authenticated layout is accepted.
    attempt_root = validate_authority_root(ledger, expected, "reserved")
    recorded_root = ledger["attempt_root"]
    assert isinstance(recorded_root, str)
    assert attempt_root == Path(recorded_root)
    assert repository in attempt_root.parents


@pytest.mark.parametrize(
    "drift",
    ["wrong-subtree", "outside-evidence", "worktree-other", "lock-name"],
)
def test_authority_root_rejects_noncanonical_attempt_layouts(
    tmp_path: Path, drift: str
) -> None:
    # Given: a tracked-CI ledger with one drifted authority path field.
    repository, ledger_path = _fixture(tmp_path)
    ledger, _ = load_json(ledger_path)
    evidence_root = repository / ".omo" / "evidence"
    attempt_id = ledger["attempt_id"]
    assert isinstance(attempt_id, str)
    candidate = copy.deepcopy(ledger)
    if drift == "wrong-subtree":
        wrong = evidence_root / "other" / attempt_id
        wrong.mkdir(parents=True)
        candidate["attempt_root"] = str(wrong)
    elif drift == "outside-evidence":
        wrong = repository / ".omo" / "elsewhere" / "clinic-os-phase1a-runtime"
        wrong.mkdir(parents=True)
        candidate["attempt_root"] = str(wrong)
    elif drift == "worktree-other":
        wrong = repository / "apps" / "clinic-os-phase1a-runtime" / attempt_id
        wrong.mkdir(parents=True)
        candidate["attempt_root"] = str(wrong)
    else:
        candidate["lock_path"] = str(evidence_root / "not-the-stable-lock")

    # When / Then: the drifted layout is rejected.
    with pytest.raises((TypeError, ValueError)):
        validate_authority_root(
            candidate, _authority_expected(ledger, repository), "reserved"
        )


def test_cli_snapshot_then_verify_roundtrip(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Given: the real module CLI against the fixture repository.
    repository, _ = _fixture(tmp_path)
    run_root = _run_root(tmp_path)
    report = tmp_path / "report.json"

    # When: a snapshot is published and then verified through the CLI.
    snapshot_code = current_source.main(
        [
            "snapshot",
            "--repository",
            str(repository),
            "--run-root",
            str(run_root),
            "--report",
            str(report),
        ]
    )
    assert snapshot_code == 0
    summary = capsys.readouterr().out
    assert "snapshot_manifest_sha256" in summary
    verify_code = current_source.main(
        [
            "verify",
            "--repository",
            str(repository),
            "--record",
            str(report),
            "--run-root",
            str(run_root),
        ]
    )

    # Then: both commands succeed and staging is cleaned.
    assert verify_code == 0
    assert list(run_root.iterdir()) == []
