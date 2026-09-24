from __future__ import annotations

from pathlib import Path

from ops.testing.validate_action_pins import validate_repository

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = PROJECT_ROOT / ".github/workflows/ci.yml"
SNAPSHOT = (
    "uv run --locked python ops/testing/isolation_ledger.py snapshot --approved-plan "
    '"$GITHUB_WORKSPACE/docs/plans/clinic-os-phase1a-approved.md" '
    "--tracked-ci-sidecar "
    '"$GITHUB_WORKSPACE/docs/plans/clinic-os-phase1a-approved.sha256" '
    "--foundation-sha cffbb1900ae2132560f20c27fcf1a514a1ef71aa "
    '--worktree "$GITHUB_WORKSPACE"'
)


def test_hosted_jobs_are_snapshot_first_and_service_free() -> None:
    # Given: the tracked hosted workflow consumed by every CI run.
    source = WORKFLOW.read_text(encoding="utf-8")

    # When / Then: no runner service exists and every first command is the snapshot.
    assert "\n    services:" not in source
    provenance = validate_repository(PROJECT_ROOT)
    assert len(provenance) == 5
    assert source.count(f"run: {SNAPSHOT}") == 7


def test_database_jobs_source_the_claimed_postgres_environment() -> None:
    # Given: the repository-owned PostgreSQL lifecycle entrypoint.
    script = PROJECT_ROOT / "ops/testing/ci_postgres.sh"
    source = WORKFLOW.read_text(encoding="utf-8")

    # When / Then: every database lane uses its private exported environment.
    assert script.is_file()
    assert script.stat().st_mode & 0o111
    assert source.count("ops/testing/ci_postgres.sh up") == 4
    assert (
        source.count('source "$RUNNER_TEMP/clinic-phase1a-ci-postgres/ci-postgres.env"')
        == 4
    )
    assert source.count("trap 'ops/testing/ci_postgres.sh down'") == 4
    assert source.index("trap 'ops/testing/ci_postgres.sh down'") < source.index(
        "ops/testing/ci_postgres.sh up"
    )
