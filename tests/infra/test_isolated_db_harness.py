from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Final

PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
HARNESS: Final = PROJECT_ROOT / "ops" / "testing" / "isolated_db.sh"
COMMAND_PROBE: Final = Path(__file__).with_name("isolation_command_probe.py")
CLAIM_ID: Final = "8dce8b9b-6db5-41b6-9942-4298c15c26f7"


def test_isolated_database_harness_surface_exists() -> None:
    # Given: Todo 1 starts from the immutable foundation tree.
    expected_paths = (
        PROJECT_ROOT / "ops" / "testing" / "isolated_db.sh",
        PROJECT_ROOT / "ops" / "testing" / "cgroup_capability_probe.py",
    )

    # When: the isolated execution surface is inspected before implementation.
    missing_paths = [
        path.relative_to(PROJECT_ROOT) for path in expected_paths if not path.is_file()
    ]

    # Then: the task cannot pass until both guarded entrypoints exist.
    assert missing_paths == []


def test_isolated_database_status_uses_claim_gate_and_ignores_dotenv(
    tmp_path: Path,
) -> None:
    # Given: inert ledger and Docker probes plus one valid reserved stack identity.
    report = tmp_path / "commands.jsonl"
    environment = os.environ.copy()
    environment.update(
        {
            "CLINIC_DOCKER_BIN": str(COMMAND_PROBE),
            "CLINIC_ISOLATION_CLAIM_ID": CLAIM_ID,
            "CLINIC_ISOLATION_COMMAND_REPORT": str(report),
            "CLINIC_LEDGER_PYTHON": str(COMMAND_PROBE),
            "COMPOSE_PROJECT_NAME": "clinic_phase1a_test_status",
            "POSTGRES_DATA_VOLUME": "clinic_phase1a_test_status_data",
            "POSTGRES_HOST_PORT": "55432",
        }
    )

    # When: the real harness performs a read-only status operation.
    result = subprocess.run(  # noqa: S603 - fixed harness and closed test environment.
        (HARNESS, "status"),
        cwd=PROJECT_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    # Then: the active claim is refreshed before Compose reads an env-file-free project.
    assert result.returncode == 0, result.stderr
    records = [json.loads(line) for line in report.read_text().splitlines()]
    assert records[0]["kind"] == "ledger"
    assert records[0]["argv"][1:] == ["verify", "--refresh", "--claim", CLAIM_ID]
    assert records[1] == {
        "argv": [
            "compose",
            "--env-file",
            "/dev/null",
            "--file",
            str(PROJECT_ROOT / "docker-compose.yml"),
            "--project-name",
            "clinic_phase1a_test_status",
            "ps",
            "db",
        ],
        "kind": "docker",
    }


def test_isolated_database_up_activates_the_reserved_live_stack(
    tmp_path: Path,
) -> None:
    # Given: inert ledger and Docker probes plus one pre-reserved stack identity.
    report = tmp_path / "commands.jsonl"
    environment = os.environ.copy()
    environment.update(
        {
            "CLINIC_DOCKER_BIN": str(COMMAND_PROBE),
            "CLINIC_ISOLATION_CLAIM_ID": CLAIM_ID,
            "CLINIC_ISOLATION_COMMAND_REPORT": str(report),
            "CLINIC_LEDGER_PYTHON": str(COMMAND_PROBE),
            "COMPOSE_PROJECT_NAME": "clinic_phase1a_test_up",
            "POSTGRES_DATA_VOLUME": "clinic_phase1a_test_up_data",
            "POSTGRES_HOST_PORT": "55433",
        }
    )

    # When: the real harness creates the exact reserved Compose stack.
    result = subprocess.run(  # noqa: S603 - fixed harness and closed test environment.
        (HARNESS, "up"),
        cwd=PROJECT_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    # Then: live reconciliation activates it before the active-claim refresh gate.
    assert result.returncode == 0, result.stderr
    records = [json.loads(line) for line in report.read_text().splitlines()]
    assert [record["argv"][-1] for record in records[:2]] == ["--quiet", "db"]
    assert records[2]["kind"] == "ledger"
    assert records[2]["argv"][1:] == ["reconcile"]
    assert records[3]["kind"] == "ledger"
    assert records[3]["argv"][1:] == ["verify", "--refresh", "--claim", CLAIM_ID]


def test_failed_up_retains_claimed_resources_for_journaled_cleanup(
    tmp_path: Path,
) -> None:
    # Given: Docker creation succeeds but live ledger reconciliation fails.
    report = tmp_path / "commands.jsonl"
    environment = os.environ.copy()
    environment.update(
        {
            "CLINIC_DOCKER_BIN": str(COMMAND_PROBE),
            "CLINIC_ISOLATION_CLAIM_ID": CLAIM_ID,
            "CLINIC_ISOLATION_COMMAND_REPORT": str(report),
            "CLINIC_LEDGER_PYTHON": "/bin/false",
            "COMPOSE_PROJECT_NAME": "clinic_phase1a_test_failed_up",
            "POSTGRES_DATA_VOLUME": "clinic_phase1a_test_failed_up_data",
            "POSTGRES_HOST_PORT": "55434",
        }
    )

    # When: the real harness reaches that post-create failure boundary.
    result = subprocess.run(  # noqa: S603 - fixed harness and closed test environment.
        (HARNESS, "up"),
        cwd=PROJECT_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    # Then: it never stops/removes before an owner records remove-intent.
    assert result.returncode != 0
    records = [json.loads(line) for line in report.read_text().splitlines()]
    assert all("down" not in record["argv"] for record in records)


def test_isolated_database_down_reconciles_absence_once(tmp_path: Path) -> None:
    # Given: inert ledger and Docker probes representing one active stack.
    report = tmp_path / "commands.jsonl"
    environment = os.environ.copy()
    environment.update(
        {
            "CLINIC_DOCKER_BIN": str(COMMAND_PROBE),
            "CLINIC_ISOLATION_CLAIM_ID": CLAIM_ID,
            "CLINIC_ISOLATION_COMMAND_REPORT": str(report),
            "CLINIC_LEDGER_PYTHON": str(COMMAND_PROBE),
            "COMPOSE_PROJECT_NAME": "clinic_phase1a_test_down",
            "POSTGRES_DATA_VOLUME": "clinic_phase1a_test_down_data",
            "POSTGRES_HOST_PORT": "55435",
        }
    )

    # When: the real harness tears down exact task-owned resources.
    result = subprocess.run(  # noqa: S603 - fixed harness and closed test environment.
        (HARNESS, "down"),
        cwd=PROJECT_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    # Then: one absence reconciliation releases the claim without a stale release.
    assert result.returncode == 0, result.stderr
    records = [json.loads(line) for line in report.read_text().splitlines()]
    ledger_argv = [
        record["argv"][1:] for record in records if record["kind"] == "ledger"
    ]
    assert ledger_argv == [
        ["verify", "--refresh", "--claim", CLAIM_ID],
        ["reconcile"],
    ]


def test_compose_and_make_expose_only_parameterized_isolated_database_inputs() -> None:
    # Given: the repository Compose and Make entrypoints used by the guarded harness.
    compose = (PROJECT_ROOT / "docker-compose.yml").read_text()
    makefile = (PROJECT_ROOT / "Makefile").read_text()

    # When: their database image, host binding, and named volume contracts are read.
    required_compose = {
        "    image: ${POSTGRES_IMAGE:-postgres:16}",
        '      - "${POSTGRES_HOST_IP:-127.0.0.1}:${POSTGRES_HOST_PORT:-5432}:5432"',
        "    name: ${POSTGRES_DATA_VOLUME:-clinic_postgres_data}",
    }
    required_make = {
        "isolated-db-up:",
        "isolated-db-status:",
        "isolated-db-down:",
    }

    # Then: task-owned host resources vary while container port 5432 stays fixed.
    assert required_compose.issubset(compose.splitlines())
    assert required_make.issubset(makefile.splitlines())


def test_compose_labels_every_isolated_database_resource_with_claim_intent() -> None:
    # Given: the Compose definition used only after a stack claim is reserved.
    compose = (PROJECT_ROOT / "docker-compose.yml").read_text()
    intent_label = "clinic.phase1a.claim: ${CLINIC_ISOLATION_CLAIM_ID:-unclaimed}"

    # When: container, volume, and network declarations are inspected.
    occurrences = [line.strip() for line in compose.splitlines()].count(intent_label)

    # Then: all three cleanup identities carry the same deterministic intent label.
    assert occurrences == 3
