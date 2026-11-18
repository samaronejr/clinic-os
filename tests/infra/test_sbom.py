"""The committed CycloneDX runtime SBOM tracks the locked runtime closure."""

from __future__ import annotations

import json
import tomllib
from typing import Any

import pytest
from ops.release import sbom


def _lock() -> dict[str, Any]:
    return {
        "package": [
            {
                "name": sbom.PROJECT_NAME,
                "version": "0.1.0",
                "dependencies": [{"name": "web"}, {"name": "db", "extra": ["fast"]}],
                "dev-dependencies": {"dev": [{"name": "lint"}]},
            },
            {"name": "web", "version": "1.0", "dependencies": [{"name": "util"}]},
            {
                "name": "db",
                "version": "2.0",
                "optional-dependencies": {
                    "fast": [{"name": "native"}],
                    "slow": [{"name": "unused"}],
                },
            },
            {"name": "util", "version": "3.0"},
            {"name": "native", "version": "4.0"},
            {"name": "unused", "version": "5.0"},
            {"name": "lint", "version": "6.0"},
        ]
    }


def _bom(timestamp: str, serial: str, names: list[str]) -> dict[str, Any]:
    versions = sbom.runtime_closure(_lock())
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "serialNumber": serial,
        "metadata": {"timestamp": timestamp, "tools": [{"name": "uv"}]},
        "components": [
            {"name": name, "version": versions[name], "purl": f"pkg:pypi/{name}"}
            for name in names
        ],
    }


def test_committed_runtime_sbom_matches_the_locked_runtime_closure() -> None:
    assert sbom.check() == []
    committed = json.loads((sbom.PROJECT_ROOT / sbom.SBOM_PATH).read_text("utf-8"))
    lock = tomllib.loads((sbom.PROJECT_ROOT / "uv.lock").read_text("utf-8"))
    closure = sbom.runtime_closure(lock)
    assert (
        committed["serialNumber"] == sbom.normalize(committed, closure)["serialNumber"]
    )
    assert "timestamp" not in committed["metadata"]
    assert "pytest" not in closure
    assert "django" in closure


def test_closure_follows_requested_extras_and_ignores_dependency_groups() -> None:
    assert sbom.runtime_closure(_lock()) == {
        "db": "2.0",
        "native": "4.0",
        "util": "3.0",
        "web": "1.0",
    }


def test_normalization_drops_run_metadata_and_is_deterministic() -> None:
    closure = sbom.runtime_closure(_lock())
    names = sorted(closure, reverse=True)
    first = sbom.render(
        sbom.normalize(_bom("2026-01-01", "urn:uuid:a", names), closure)
    )
    second = sbom.render(
        sbom.normalize(_bom("2026-02-02", "urn:uuid:b", names), closure)
    )
    assert first == second
    rendered = json.loads(first)
    assert [item["name"] for item in rendered["components"]] == sorted(closure)
    assert rendered["metadata"]["properties"] == [
        {"name": sbom.CLOSURE_PROPERTY, "value": sbom.closure_digest(closure)},
        {"name": sbom.SCOPE_PROPERTY, "value": "runtime"},
    ]


def test_export_that_disagrees_with_the_lock_is_refused() -> None:
    closure = sbom.runtime_closure(_lock())
    with pytest.raises(sbom.SbomError):
        sbom.normalize(_bom("t", "s", ["web", "db"]), closure)
    with pytest.raises(sbom.SbomError):
        sbom.normalize(
            {**_bom("t", "s", sorted(closure)), "specVersion": "1.4"}, closure
        )
