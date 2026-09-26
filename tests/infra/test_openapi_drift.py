"""The committed UI API contract must equal a fresh drf-spectacular export."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import yaml
from django.core.management import call_command
from ops.testing.runtime_paths import runtime_directory

if TYPE_CHECKING:
    from collections.abc import Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONTRACT = PROJECT_ROOT / "docs" / "api" / "ui-v1.yaml"
REGENERATE = "uv run python manage.py spectacular --file docs/api/ui-v1.yaml"
UI_API_V1_PREFIX = "/api/ui/v1/"


def _generate(run_root: Path) -> bytes:
    with runtime_directory(run_root, purpose="openapi-drift") as work:
        generated = work / "ui-v1.yaml"
        call_command(
            "spectacular",
            "--file",
            str(generated),
            "--validate",
            "--fail-on-warn",
        )
        return generated.read_bytes()


def _contract() -> Mapping[str, object]:
    document = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def test_committed_contract_matches_regenerated_schema(tmp_path: Path) -> None:
    generated = _generate(tmp_path)

    assert generated == CONTRACT.read_bytes(), (
        f"docs/api/ui-v1.yaml drifted from the code; regenerate with: {REGENERATE}"
    )


def test_contract_is_post_only_with_body_identifiers_and_csrf_session() -> None:
    paths = _contract()["paths"]
    assert isinstance(paths, dict)
    assert paths, "the UI API contract must not be empty"

    for route, operations in paths.items():
        assert route.startswith(UI_API_V1_PREFIX)
        assert "{" not in route, "record identifiers belong in POST bodies"
        assert set(operations) == {"post"}
        operation = operations["post"]
        assert "parameters" not in operation
        assert operation["security"] == [{"sessionCookie": [], "csrfHeader": []}]
        for status in ("400", "403"):
            schema = operation["responses"][status]["content"]["application/json"]
            assert schema["schema"] == {"$ref": "#/components/schemas/Error"}


def test_contract_declares_the_error_body_and_csrf_header() -> None:
    components = _contract()["components"]
    assert isinstance(components, dict)

    error = components["schemas"]["Error"]
    assert error["required"] == ["code", "message_key"]
    assert set(error["properties"]) == {"code", "message_key"}
    assert components["securitySchemes"]["csrfHeader"] == {
        "type": "apiKey",
        "in": "header",
        "name": "X-CSRFToken",
    }
