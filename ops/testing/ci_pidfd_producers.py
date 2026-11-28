"""Select immutable artifact IDs from the effective GitHub job executions."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Never, TypedDict, cast

import yaml

from ops.testing.ci_pytest_partition import PYTHONS, SHARDS
from ops.testing.isolation_common import IsolationError

if TYPE_CHECKING:
    from collections.abc import Sequence


class Producer(TypedDict):
    """Authenticated Actions identity for one uploaded artifact."""

    attempt: int
    job_id: int
    artifact_id: int


SINGLETONS = ("contracts", "worker-integration", "migration-upgrade", "actionlint")


def _fail(message: str) -> Never:
    raise IsolationError(message)


def artifact_jobs(workflow: str) -> dict[str, str]:
    """Map the existing artifact names to the concrete Actions job names."""
    jobs = yaml.safe_load(workflow)["jobs"]
    names: dict[str, str] = {f"renewal-{job}": jobs[job]["name"] for job in SINGLETONS}
    coverage_name = jobs["python-coverage"]["name"]
    for version in PYTHONS:
        names[f"renewal-coverage-py{version}"] = coverage_name.replace(
            "${{ matrix.python-version }}", version
        )
        for shard in SHARDS:
            names[f"renewal-python-py{version}-shard-{shard}"] = (
                jobs["test"]["name"]
                .replace("${{ matrix.python-version }}", version)
                .replace("${{ matrix.shard }}", str(shard))
            )
    browser = jobs["renewal-browser"]
    for index, shard_set in enumerate(browser["strategy"]["matrix"]["shard"]):
        names[f"renewal-browser-{index}"] = browser["name"].replace(
            "${{ matrix.shard }}", shard_set
        )
    return names


def select_producers(
    names: dict[str, str],
    attempts: Sequence[list[dict[str, object]]],
    artifacts: list[dict[str, object]],
    *,
    run_id: int,
    head: str,
) -> dict[str, Producer]:
    """Bind each effective job's artifact to its real execution window."""
    origins: dict[tuple[object, object], tuple[int, object]] = {}
    for number, jobs in enumerate(attempts, 1):
        for historical in jobs:
            # GitHub can allocate a new job ID when projecting a successful job
            # into a later attempt. The original execution's start is unchanged.
            origins.setdefault(
                (historical["name"], historical["started_at"]),
                (number, historical["id"]),
            )
    effective = {job["name"]: job for job in attempts[-1]}
    selected: dict[str, Producer] = {}
    for artifact, name in names.items():
        job = effective.get(name)
        if job is None or job["run_id"] != run_id or job["head_sha"] != head:
            _fail(f"missing effective Actions job for {artifact}")
        started, finished = job["started_at"], job["completed_at"]
        if not isinstance(started, str) or not isinstance(finished, str):
            _fail(f"missing Actions execution window for {artifact}")
        matches = [
            item
            for item in artifacts
            if item["name"] == artifact
            and not item["expired"]
            and started <= str(item["created_at"]) <= finished
            and isinstance(item["workflow_run"], dict)
            and item["workflow_run"].get("id") == run_id
            and item["workflow_run"].get("head_sha") == head
        ]
        if len(matches) != 1:
            _fail(f"expected one Actions artifact for {artifact}")
        attempt, job_id = origins[name, started]
        artifact_id = matches[0]["id"]
        if type(job_id) is not int or type(artifact_id) is not int:
            _fail(f"invalid Actions identity for {artifact}")
        selected[artifact] = {
            "attempt": attempt,
            "job_id": job_id,
            "artifact_id": artifact_id,
        }
    return selected


def _api(endpoint: str) -> dict[str, object]:
    gh = shutil.which("gh")
    if gh is None:
        _fail("Actions metadata requires gh")
    process = subprocess.run(  # noqa: S603 - fixed CLI and same-repository endpoint.
        (gh, "api", endpoint),
        check=True,
        capture_output=True,
        timeout=45,
    )
    return cast("dict[str, object]", json.loads(process.stdout))


def _items(endpoint: str, field: str) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    number = 1
    while True:
        page = _api(f"{endpoint}?per_page=100&page={number}")
        new = cast("list[dict[str, object]]", page[field])
        items.extend(new)
        if page["total_count"] == len(items):
            return items
        if not new:
            _fail(f"incomplete Actions {field} inventory")
        number += 1


def main() -> None:
    """Write the producer metadata and exact artifact IDs for hosted acceptance."""
    workflow, metadata, outputs = map(Path, sys.argv[1:])
    run_id = int(os.environ["GITHUB_RUN_ID"])
    attempt = int(os.environ["GITHUB_RUN_ATTEMPT"])
    endpoint = f"repos/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{run_id}"
    run = _api(endpoint)
    head = run["head_sha"]
    if (
        run["id"] != run_id
        or run["run_attempt"] != attempt
        or not isinstance(head, str)
    ):
        _fail("Actions run does not match the executing attempt")
    attempts = [
        _items(f"{endpoint}/attempts/{number}/jobs", "jobs")
        for number in range(1, attempt + 1)
    ]
    producers = select_producers(
        artifact_jobs(workflow.read_text(encoding="utf-8")),
        attempts,
        _items(f"{endpoint}/artifacts", "artifacts"),
        run_id=run_id,
        head=head,
    )
    with metadata.open("x", encoding="utf-8") as stream:
        json.dump(
            {
                "run_id": str(run_id),
                "attempt": attempt,
                "revision": os.environ["GITHUB_SHA"],
                "artifacts": producers,
            },
            stream,
        )
    with outputs.open("a", encoding="utf-8") as stream:
        ids = ",".join(str(producer["artifact_id"]) for producer in producers.values())
        stream.write(f"artifact-ids={ids}\n")


if __name__ == "__main__":
    main()
