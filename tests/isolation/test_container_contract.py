from __future__ import annotations

import json
import re
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

import pytest
import rfc8785
from ops.testing.candidate_pair import validate_candidate_pair
from ops.testing.image_source import assemble_candidate_context
from ops.testing.isolation_candidate_contract import envelope_binding
from ops.testing.process_helpers import run_process

if TYPE_CHECKING:
    from ops.testing.isolation_common import JsonObject

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PYTHON_IMAGE = (
    "docker.io/library/python:3.13.14-slim-bookworm@"
    "sha256:dd86541a59b252667f4c12f8b2ee17216de37dd65ac773bf097bef996fa78860"
)
UV_IMAGE = (
    "ghcr.io/astral-sh/uv:0.10.6@"
    "sha256:2f2ccd27bbf953ec7a9e3153a4563705e41c852a5e1912b438fc44d88d6cb52c"
)
DEFAULT_COMMAND = [
    "gunicorn",
    "--config=/app/ops/container/gunicorn_no_proxy.py",
    "--bind=0.0.0.0:8000",
    "--workers=2",
    "--threads=4",
    "--timeout=30",
    "--graceful-timeout=30",
    "--keep-alive=5",
    "--max-requests=1000",
    "--max-requests-jitter=100",
    "--access-logfile=-",
    "--error-logfile=-",
    "config.wsgi:application",
]


def _git(repository: Path, *arguments: str) -> str:
    result = run_process(("/usr/bin/git", "-C", str(repository), *arguments))
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


def test_application_dockerfile_is_immutable_nonroot_and_apt_free() -> None:
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text()
    from_lines = [line for line in dockerfile.splitlines() if line.startswith("FROM ")]

    assert from_lines == [
        f"FROM --platform=linux/amd64 {UV_IMAGE} AS uv",
        f"FROM --platform=linux/amd64 {PYTHON_IMAGE} AS builder",
        f"FROM --platform=linux/amd64 {PYTHON_IMAGE} AS runtime",
    ]
    assert re.search(r"\bapt(?:-get)?\b", dockerfile, re.IGNORECASE) is None
    assert "ARG TARGETARCH" in dockerfile
    assert 'test "$TARGETARCH" = "amd64"' in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert "STOPSIGNAL SIGTERM" in dockerfile
    assert 'ENTRYPOINT ["/app/ops/container/entrypoint.sh"]' in dockerfile
    command_line = next(
        line.removeprefix("CMD ")
        for line in dockerfile.splitlines()
        if line.startswith("CMD ")
    )
    assert json.loads(command_line) == DEFAULT_COMMAND
    assert 'PYTHONTZPATH=""' in dockerfile
    assert 'clinic.phase1a.python-version="3.13.14"' in dockerfile
    assert 'clinic.phase1a.uv-version="0.10.6"' in dockerfile
    assert 'clinic.phase1a.tzdata-version="2026.3"' in dockerfile
    assert (
        "RUN /app/.venv/bin/python -m ops.testing.timezone_contract uv.lock && "
        "DJANGO_SETTINGS_MODULE=config.settings.base "
        "/app/.venv/bin/python manage.py collectstatic --noinput"
    ) in dockerfile
    bare_python_lines = [
        line
        for line in dockerfile.splitlines()
        if re.search(r"(^|\s)python\s", line) and "/app/.venv/bin/python" not in line
    ]
    assert len(bare_python_lines) == 2
    assert all("sys.version_info[:3]==(3,13,14)" in line for line in bare_python_lines)

    gunicorn_config = PROJECT_ROOT / "ops/container/gunicorn_no_proxy.py"
    assert gunicorn_config.read_bytes() == (
        b'forwarded_allow_ips = ""\n'
        b"secure_scheme_headers = {}\n"
        b"# ADR-014: access logs carry method + status + duration only; the request\n"
        b"# line, query, remote address and headers are never logged (PHI boundary).\n"
        b'access_log_format = "%(m)s %(s)s %(D)s"\n'
    )
    entrypoint = (PROJECT_ROOT / "ops/container/entrypoint.sh").read_text()
    assert "migrate" not in entrypoint
    assert 'exec "$@"' in entrypoint
    start = (PROJECT_ROOT / "ops/container/start.sh").read_text()
    assert re.search(r"(^|\s)(python|pip)(\s|$)", entrypoint + start) is None


def test_application_context_is_assembled_from_clean_git_objects(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    files = {
        "Dockerfile": "FROM scratch\n",
        "apps/example.py": "VALUE = 1\n",
        "manage.py": "print('fixture')\n",
        "ops/testing/application-image.dockerignore": "**\n",
        "pyproject.toml": "[project]\nname='fixture'\nversion='1'\n",
        "tests/excluded.py": "raise AssertionError\n",
        "uv.lock": "version = 1\n",
    }
    for relative_path, content in files.items():
        path = repository / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    _git(repository, "init", "--quiet")
    _git(repository, "add", ".")
    _git(
        repository,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@invalid.example",
        "commit",
        "--quiet",
        "-m",
        "fixture",
    )
    revision = _git(repository, "rev-parse", "HEAD")
    destination = tmp_path / "context"

    contract = assemble_candidate_context(
        repository,
        destination,
        revision,
        "application",
    )

    relative_paths = sorted(
        str(path.relative_to(destination))
        for path in destination.rglob("*")
        if path.is_file()
    )
    assert relative_paths == [
        ".dockerignore",
        "Dockerfile",
        "application-source-manifest.json",
        "apps/example.py",
        "manage.py",
        "pyproject.toml",
        "uv.lock",
    ]
    assert contract["kind"] == "application"
    assert contract["revision_sha"] == revision
    assert contract["available_suite_ids"] == []
    (repository / "untracked").write_text("dirty\n")
    with pytest.raises(RuntimeError, match="candidate source contract failed"):
        assemble_candidate_context(
            repository,
            tmp_path / "dirty",
            revision,
            "application",
        )


def test_runner_context_rejects_a_missing_required_dockerfile(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    for relative_path in (
        "ops/testing/browser-runner.dockerignore",
        "pyproject.toml",
        "uv.lock",
    ):
        path = repository / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture\n")
    _git(repository, "init", "--quiet")
    _git(repository, "add", ".")
    _git(
        repository,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@invalid.example",
        "commit",
        "--quiet",
        "-m",
        "fixture",
    )
    revision = _git(repository, "rev-parse", "HEAD")

    with pytest.raises(RuntimeError, match="candidate source contract failed"):
        assemble_candidate_context(
            repository,
            tmp_path / "runner-context",
            revision,
            "browser-runner",
        )


def test_image_smoke_cli_rejects_noncanonical_forms() -> None:
    script = PROJECT_ROOT / "ops/testing/image_smoke.sh"
    invalid_forms = (
        (),
        ("build",),
        ("build", "--sha", "abc"),
        ("smoke", "--sha", "1" * 40, "extra"),
        ("unknown", "--sha", "1" * 40),
    )

    for arguments in invalid_forms:
        result = run_process((str(script), *arguments))
        assert result.returncode == 2
        assert "image-smoke: invalid invocation" in result.stderr


def test_candidate_pair_validates_binding_history_and_released_publisher() -> None:
    claim_id = "11111111-1111-4111-8111-111111111111"
    envelope: JsonObject = {
        "attempt_id": "22222222-2222-4222-8222-222222222222",
        "authorization_id": "candidate-application-envelope",
        "claim_id": claim_id,
        "image_contract": {
            "available_suite_ids": [],
            "kind": "application",
            "revision_sha": "3" * 40,
            "source_entry_count": 1,
            "source_manifest_sha256": "4" * 64,
            "tree_sha": "5" * 40,
        },
        "image_id": f"sha256:{'6' * 64}",
        "published_at_utc": "2026-08-17T00:00:00.000000Z",
        "revision_sha": "3" * 40,
        "schema_version": 1,
        "tree_sha": "5" * 40,
    }
    binding = envelope_binding(envelope)
    temporary_root = PurePosixPath("/", "tmp")
    history: JsonObject = {
        "attempt_id": envelope["attempt_id"],
        "authorization_id": "candidate-application-publication-history",
        "candidate_envelope_binding_sha256": sha256(rfc8785.dumps(binding)).hexdigest(),
        "claim_id": claim_id,
        "output_kind": "publication-history",
        "predecessor_authorization_id": "candidate-application-envelope",
        "predecessor_entries_sha256": "7" * 64,
        "predecessor_root_path": str(temporary_root / "candidate-images"),
        "purpose": "candidate-application-publisher",
        "relative_path": f"{claim_id}.json",
        "root_path": str(temporary_root / "publication-history"),
        "schema_version": 1,
    }
    envelope_raw = rfc8785.dumps(envelope) + b"\n"
    history_raw = rfc8785.dumps(history) + b"\n"

    validate_candidate_pair(envelope_raw, history_raw, [])
    with pytest.raises(RuntimeError, match="candidate pair contract failed"):
        validate_candidate_pair(envelope_raw, history_raw, [{"claim_id": claim_id}])


def test_candidate_dockerfiles_use_the_ledger_verifier_label_names() -> None:
    application = (PROJECT_ROOT / "Dockerfile").read_text()
    runner = (PROJECT_ROOT / "ops/testing/browser-runner.Dockerfile").read_text()

    for source in (application, runner):
        assert 'org.opencontainers.image.revision="$CLINIC_REVISION_SHA"' in source
        assert 'clinic.phase1a.tree="$CLINIC_TREE_SHA"' in source
        assert "clinic.phase1a.image-kind=" in source
    assert "clinic.phase1a.application-source-sha256" in application
    assert "clinic.phase1a.application-source-entry-count" in application
    assert "clinic.phase1a.runner-source-sha256" in runner
    assert "clinic.phase1a.runner-source-entry-count" in runner
    assert "clinic.phase1a.available-suite-ids" in runner
