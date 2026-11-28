"""Run the Make CI gates inside one fail-closed interpreter boundary."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import sys
from pathlib import Path

from ops.testing.pidfd_preflight import require_pidfd_at_startup

BROWSER_SUITES_DIGEST = (
    "4c6e14cdf93690d1c38bba4b5a7c36cf657bf795bc3a003a17ad5bc3e67067c5"
)


async def _run(*argv: str, capture: bool = False) -> str:
    process = await asyncio.create_subprocess_exec(
        *argv,
        env={**os.environ, "UV_PROJECT_ENVIRONMENT": sys.prefix},
        stdout=asyncio.subprocess.PIPE if capture else None,
    )
    stdout, _ = await process.communicate()
    if process.returncode:
        code = process.returncode
        raise SystemExit(code if code > 0 else 128 - code)
    return stdout.decode().strip() if capture else ""


async def _browser() -> None:
    path = Path("ops/testing/ci-required-browser-suites.txt")
    manifest = await asyncio.to_thread(path.read_bytes)
    suites = ("availability", "patient", "scheduling")
    if (
        hashlib.sha256(manifest).hexdigest() != BROWSER_SUITES_DIGEST
        or tuple(manifest.decode().splitlines()) != suites
    ):
        message = "ci-local: invalid required browser suite manifest"
        raise SystemExit(message)
    options = [item for suite in suites for item in ("--require-suite", suite)]
    await _run("ops/testing/browser_runner.sh", "probe", *options)
    await _run(
        sys.executable,
        "-m",
        "ops.testing.renewal_runner",
        "browser",
        "--suite",
        "clinical-inbox",
    )


async def _image() -> None:
    await _run(sys.executable, "-m", "ops.testing.ci_image_contracts")
    sha = await _run("git", "rev-parse", "HEAD", capture=True)
    await _run("ops/testing/image_smoke.sh", "build", "--sha", sha)
    await _run("ops/testing/tls_stack.sh", "smoke")


async def _quality(make: str, workers: str) -> None:
    for target in ("db-bootstrap", "migrate", "db-posture"):
        await _run(make, target)
    await _run(sys.executable, "-m", "ruff", "check", ".")
    await _run(sys.executable, "-m", "ruff", "format", "--check", ".")
    await _run(sys.executable, "-m", "mypy", ".")
    await _run(
        sys.executable,
        "-m",
        "ops.testing.ci_pytest",
        "--workers",
        workers,
        "--work-root",
        str(Path.cwd() / "var/ci-pytest"),
    )
    await _run(sys.executable, "-m", "pip_audit", "--local")
    await _image()
    await _browser()


def main() -> None:
    """Refuse unsupported interpreters before any contract work is dispatched."""
    require_pidfd_at_startup()
    parser = argparse.ArgumentParser(description=__doc__)
    gates = parser.add_subparsers(dest="gate", required=True)
    gates.add_parser("browser")
    gates.add_parser("image")
    quality = gates.add_parser("quality")
    quality.add_argument("--make", default="make")
    quality.add_argument("--workers", required=True)
    args = parser.parse_args()
    if args.gate == "browser":
        asyncio.run(_browser())
    elif args.gate == "image":
        asyncio.run(_image())
    else:
        asyncio.run(_quality(args.make, args.workers))


if __name__ == "__main__":
    main()
