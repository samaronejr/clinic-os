"""Dispatch hosted singleton workloads behind an in-process pidfd check."""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

import pytest

from ops.testing.pidfd_preflight import require_pidfd_at_startup, write_hosted_record
from ops.testing.validate_action_pins import main as action_pins


async def _run(*argv: str) -> int:
    process = await asyncio.create_subprocess_exec(*argv)
    return await process.wait()


def _contracts() -> int:
    for command in (
        ("ops/testing/image_smoke.sh", "build", "--sha", os.environ["GITHUB_SHA"]),
        (
            "ops/testing/browser_runner.sh",
            "probe",
            "--require-suite",
            "availability",
            "--require-suite",
            "patient",
            "--require-suite",
            "scheduling",
        ),
    ):
        status = asyncio.run(_run(*command))
        if status != 0:
            return status
    status = pytest.main(
        [
            "-q",
            "--reuse-db",
            "tests/infra/test_schema_policy.py",
            "tests/audit/test_phase1_audit_migration.py",
            "tests/browser/test_browser_runner_contract.py",
            "tests/isolation/test_container_contract.py",
        ]
    )
    if status:
        return status
    return asyncio.run(_run("ops/testing/tls_stack.sh", "smoke"))


def main() -> int:
    """Record the actual gate interpreter, then run the named workload."""
    capabilities = require_pidfd_at_startup()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "job",
        choices=("contracts", "worker-integration", "migration-upgrade", "actionlint"),
    )
    parser.add_argument("artifact", type=Path)
    parser.add_argument("--base-sha")
    args = parser.parse_args()
    if args.job == "migration-upgrade" and args.base_sha is None:
        parser.error("migration-upgrade requires --base-sha")
    write_hosted_record(args.artifact, args.job, capabilities)
    match args.job:
        case "contracts":
            return _contracts()
        case "worker-integration":
            return pytest.main(
                [
                    "-q",
                    "--reuse-db",
                    f"--junitxml={args.artifact / 'junit.xml'}",
                    "tests/renewal/test_broker_delivery.py",
                    "tests/resilience/test_broker_worker_death.py",
                    "tests/realtime/",
                ]
            )
        case "migration-upgrade":
            return asyncio.run(_run("ops/testing/migration_upgrade.sh", args.base_sha))
        case _:
            return action_pins()


if __name__ == "__main__":
    raise SystemExit(main())
