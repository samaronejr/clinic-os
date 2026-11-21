"""New validator imports and application modules cannot escape the identity."""

from __future__ import annotations

import ast
import json
from pathlib import Path

from apps.ai.evaluation import versions
from apps.ai.evaluation.protocol import ENGINEERING_THRESHOLDS, threshold_hash


def test_reachable_application_imports_from_output_validation_are_governed() -> None:
    root = Path(__file__).resolve().parents[2]
    pending = ["apps.ai.output_validation"]
    visited: set[str] = set()
    reachable: set[str] = set()
    while pending:
        module = pending.pop()
        if module in visited:
            continue
        visited.add(module)
        path = root / (module.replace(".", "/") + ".py")
        if not path.is_file():
            path = root / module.replace(".", "/") / "__init__.py"
        if not path.is_file():
            continue
        reachable.add(path.relative_to(root).as_posix())
        for node in ast.walk(ast.parse(path.read_bytes())):
            if isinstance(node, ast.Import):
                pending.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                pending.append(node.module)
                pending.extend(node.module + "." + alias.name for alias in node.names)
    assert "apps/ai/care_contracts.py" in reachable
    assert "apps/analytics/analyst_contracts.py" in reachable
    assert "apps/analytics/contracts.py" in reachable
    assert reachable <= set(versions.governed_paths())


def test_governed_boundary_covers_every_new_module_and_versioned_resource() -> None:
    root = Path(__file__).resolve().parents[2]
    actual = set(versions.governed_paths())
    assert {
        path.relative_to(root).as_posix() for path in (root / "apps").rglob("*.py")
    } <= actual
    assert "apps/ai/care_coordination.py" in actual
    assert "apps/ai/fixtures/care-coordination-v1.json" in actual
    assert {"uv.lock", "pyproject.toml"} <= actual


def test_committed_engineering_preregistration_matches_the_executed_rubric() -> None:
    registration = json.loads(
        (Path(versions.__file__).parent / "engineering-registration.json").read_bytes()
    )
    assert registration["thresholds"] == ENGINEERING_THRESHOLDS
    assert registration["thresholds_sha256"] == threshold_hash(ENGINEERING_THRESHOLDS)
    assert registration["external_gates_satisfied"] == []
