"""Generate and check the committed CycloneDX runtime SBOM (todo 73).

``uv export --format cyclonedx1.5`` lists the locked runtime closure (no
dependency groups). This module removes the run-dependent fields (timestamp,
random serial number), sorts the components and records a digest of the
closure, so the committed file is a pure function of ``uv.lock``. Regenerate
it whenever the runtime closure changes, and cite its SHA-256 in each release
record::

    python -m ops.release.sbom write   # rewrites SBOM_PATH from uv.lock
    python -m ops.release.sbom check   # exit 1 if SBOM_PATH has drifted

``runtime_closure`` recomputes the same closure from ``uv.lock`` without uv,
which is the oracle the check (and tests/infra/test_sbom.py) compares against.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tomllib
import uuid
from pathlib import Path
from typing import Any, Final, Never

PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
SBOM_PATH: Final = "docs/releases/sbom/clinic-os-runtime.cdx.json"
PROJECT_NAME: Final = "clinic-os-foundation"
CLOSURE_PROPERTY: Final = "clinic:runtime-closure-sha256"
SCOPE_PROPERTY: Final = "clinic:scope"
EXPORT: Final = (
    "export",
    "--format",
    "cyclonedx1.5",
    "--frozen",
    "--no-default-groups",
    "--preview-features",
    "sbom-export",
)


class SbomError(RuntimeError):
    """The SBOM cannot be produced or no longer matches the lock."""


def _fail(message: str) -> Never:
    raise SbomError(message)


def runtime_closure(lock: dict[str, Any]) -> dict[str, str]:
    """Return ``name -> version`` for the project's locked runtime closure."""
    packages = {package["name"]: package for package in lock["package"]}
    if PROJECT_NAME not in packages:
        _fail(f"uv.lock has no {PROJECT_NAME} package")
    extras: dict[str, set[str]] = {}
    pending = [
        (item["name"], tuple(item.get("extra", ())))
        for item in packages[PROJECT_NAME].get("dependencies", ())
    ]
    while pending:
        name, wanted = pending.pop()
        package = packages[name]
        first = name not in extras
        new = set(wanted) - extras.get(name, set())
        extras.setdefault(name, set()).update(wanted)
        edges = list(package.get("dependencies", ())) if first else []
        for extra in sorted(new):
            edges.extend(package.get("optional-dependencies", {}).get(extra, ()))
        pending.extend((edge["name"], tuple(edge.get("extra", ()))) for edge in edges)
    return {name: str(packages[name]["version"]) for name in sorted(extras)}


def closure_digest(closure: dict[str, str]) -> str:
    """Return the SHA-256 of the closure's canonical ``name==version`` lines."""
    text = "".join(f"{name}=={version}\n" for name, version in sorted(closure.items()))
    return hashlib.sha256(text.encode()).hexdigest()


def normalize(bom: dict[str, Any], closure: dict[str, str]) -> dict[str, Any]:
    """Return a deterministic SBOM whose components equal ``closure``."""
    if bom.get("bomFormat") != "CycloneDX" or bom.get("specVersion") != "1.5":
        _fail("uv export did not produce a CycloneDX 1.5 document")
    components = sorted(bom.get("components", ()), key=lambda item: item["name"])
    listed = {item["name"]: item["version"] for item in components}
    if listed != closure:
        _fail("uv export components differ from the uv.lock runtime closure")
    digest = closure_digest(closure)
    metadata = {
        key: value for key, value in bom["metadata"].items() if key != "timestamp"
    }
    metadata["properties"] = [
        {"name": CLOSURE_PROPERTY, "value": digest},
        {"name": SCOPE_PROPERTY, "value": "runtime"},
    ]
    serial = uuid.uuid5(uuid.NAMESPACE_URL, f"clinic-os-sbom:{digest}")
    return {
        **bom,
        "serialNumber": f"urn:uuid:{serial}",
        "metadata": metadata,
        "components": components,
    }


def render(bom: dict[str, Any]) -> bytes:
    """Return the committed byte form (indented, key-sorted, trailing newline)."""
    return (json.dumps(bom, indent=2, sort_keys=True) + "\n").encode()


def generate(root: Path = PROJECT_ROOT) -> bytes:
    """Export the runtime SBOM through uv and normalize it."""
    executable = shutil.which("uv")
    if executable is None:
        _fail("uv is not on PATH")
    exported = subprocess.run(  # noqa: S603 - fixed uv argv
        [executable, *EXPORT],
        cwd=root,
        check=True,
        capture_output=True,
    ).stdout
    lock = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    return render(normalize(json.loads(exported), runtime_closure(lock)))


def check(root: Path = PROJECT_ROOT) -> list[str]:
    """Return drift failures of the committed SBOM against ``uv.lock``."""
    path = root / SBOM_PATH
    if not path.is_file():
        return [f"{SBOM_PATH} is missing"]
    bom = json.loads(path.read_text(encoding="utf-8"))
    closure = runtime_closure(
        tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    )
    failures: list[str] = []
    listed = {item["name"]: item["version"] for item in bom.get("components", ())}
    if listed != closure:
        missing = sorted(set(closure.items()) - set(listed.items()))
        extra = sorted(set(listed.items()) - set(closure.items()))
        failures.append(f"components drifted: missing={missing} extra={extra}")
    properties = {
        item["name"]: item["value"] for item in bom["metadata"].get("properties", ())
    }
    if properties.get(CLOSURE_PROPERTY) != closure_digest(closure):
        failures.append(f"{CLOSURE_PROPERTY} does not match uv.lock")
    failures.extend(
        f"{item['name']} has no exact PyPI purl"
        for item in bom.get("components", ())
        if item.get("purl") != f"pkg:pypi/{item['name']}@{item['version']}"
    )
    return failures


def run_cli(argv: list[str] | None = None, root: Path = PROJECT_ROOT) -> int:
    """Write or check the committed SBOM."""
    parser = argparse.ArgumentParser(prog="sbom")
    parser.add_argument("command", choices=("write", "check"))
    arguments = parser.parse_args(argv)
    if arguments.command == "write":
        target = root / SBOM_PATH
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(generate(root))
        sys.stdout.write(f"wrote {SBOM_PATH}\n")
        return 0
    failures = check(root)
    for failure in failures:
        sys.stderr.write(f"sbom: {failure}\n")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
