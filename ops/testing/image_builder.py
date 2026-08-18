"""Bounded Docker build execution for candidate image contexts."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Final, Never

from ops.testing.isolation_common import IsolationError, JsonObject

if TYPE_CHECKING:
    from pathlib import Path

BUILD_TIMEOUT_SECONDS: Final = 1_800


def build_candidate_image(context: Path, contract: JsonObject, iidfile: Path) -> None:
    """Build one amd64 candidate image from its closed source context."""
    asyncio.run(_build_candidate_image(context, contract, iidfile))


async def _build_candidate_image(
    context: Path,
    contract: JsonObject,
    iidfile: Path,
) -> None:
    arguments = (
        "/usr/bin/docker",
        "build",
        "--platform=linux/amd64",
        "--file",
        str(context / "Dockerfile"),
        "--iidfile",
        str(iidfile),
        "--build-arg",
        "TARGETARCH=amd64",
        "--build-arg",
        f"CLINIC_REVISION_SHA={contract['revision_sha']}",
        "--build-arg",
        f"CLINIC_TREE_SHA={contract['tree_sha']}",
        "--build-arg",
        f"CLINIC_SOURCE_MANIFEST_SHA256={contract['source_manifest_sha256']}",
        "--build-arg",
        f"CLINIC_SOURCE_ENTRY_COUNT={contract['source_entry_count']}",
        "--build-arg",
        "CLINIC_AVAILABLE_SUITE_IDS=" + ",".join(_strings(contract)),
        str(context),
    )
    process = await asyncio.create_subprocess_exec(*arguments)
    try:
        return_code = await asyncio.wait_for(
            process.wait(),
            timeout=BUILD_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        process.kill()
        await process.wait()
        _fail("candidate image build timed out")
    if return_code != 0:
        _fail("candidate image build failed")


def _strings(contract: JsonObject) -> list[str]:
    value = contract.get("available_suite_ids")
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        _fail("candidate suite IDs are invalid")
    return [item for item in value if isinstance(item, str)]


def _fail(message: str) -> Never:
    raise IsolationError(message)
