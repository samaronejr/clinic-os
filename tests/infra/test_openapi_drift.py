"""The committed API contracts must equal fresh drf-spectacular exports."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import yaml
from django.core.management import call_command
from ops.testing.runtime_paths import runtime_directory

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONTRACT = PROJECT_ROOT / "docs" / "api" / "ui-v1.yaml"
REGENERATE = "uv run python manage.py spectacular --file docs/api/ui-v1.yaml"
UI_API_V1_PREFIX = "/api/ui/v1/"
EXTERNAL_CONTRACT = PROJECT_ROOT / "docs" / "api" / "external-v1.yaml"
EXTERNAL_OPTIONS = (
    "--urlconf",
    "apps.interop.api.schema_urls",
    "--custom-settings",
    "apps.interop.api.schema.EXTERNAL_API_SETTINGS",
)
EXTERNAL_REGENERATE = (
    "uv run python manage.py spectacular " + " ".join(EXTERNAL_OPTIONS) + " "
    "--file docs/api/external-v1.yaml --validate --fail-on-warn"
)
EXTERNAL_API_V1_PREFIX = "/api/v1/"
OAUTH_ROUTE = "/api/v1/oauth/token"
OPAQUE_PATH_IDS = {"appointment_id", "invoice_id", "patient_id"}


def _generate(run_root: Path, *options: str, name: str = "ui-v1.yaml") -> bytes:
    with runtime_directory(run_root, purpose="openapi-drift") as work:
        generated = work / name
        call_command(
            "spectacular",
            *options,
            "--file",
            str(generated),
            "--validate",
            "--fail-on-warn",
        )
        return generated.read_bytes()


def _contract(path: Path = CONTRACT) -> Mapping[str, Any]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
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


def test_committed_external_contract_matches_regenerated_schema(
    tmp_path: Path,
) -> None:
    generated = _generate(tmp_path, *EXTERNAL_OPTIONS, name="external-v1.yaml")

    assert generated == EXTERNAL_CONTRACT.read_bytes(), (
        "docs/api/external-v1.yaml drifted from the code; regenerate with: "
        + EXTERNAL_REGENERATE
    )


def _external_operations() -> list[tuple[str, str, Mapping[str, Any]]]:
    paths = _contract(EXTERNAL_CONTRACT)["paths"]
    assert isinstance(paths, dict)
    assert paths, "the external API contract must not be empty"
    return [
        (route, method, operation)
        for route, operations in paths.items()
        for method, operation in operations.items()
    ]


def test_external_contract_is_bearer_metered_with_opaque_record_ids() -> None:
    for route, method, operation in _external_operations():
        assert route.startswith(EXTERNAL_API_V1_PREFIX)
        assert method in {"get", "post"}
        responses = operation["responses"]
        for status in ("429", "503"):
            assert set(responses[status]["headers"]) == {"Retry-After"}
        parameters = operation.get("parameters", [])
        if route == OAUTH_ROUTE:
            assert operation["security"] == [{"clientBasic": []}]
            assert set(operation["requestBody"]["content"]) == {
                "application/x-www-form-urlencoded"
            }
            continue
        assert operation["security"] == [{"bearerToken": []}]
        error = {"$ref": "#/components/schemas/Error"}
        for status in ("400", "401", "403", "429", "503"):
            assert responses[status]["content"]["application/json"]["schema"] == error
        headers = [item for item in parameters if item["in"] == "header"]
        if method == "post":
            assert [(item["name"], item["required"]) for item in headers] == [
                ("Idempotency-Key", True)
            ]
        else:
            assert headers == []
        for item in parameters:
            if item["in"] == "path" and item["name"] in OPAQUE_PATH_IDS:
                assert item["schema"] == {"type": "string"}
                assert item["description"] == (
                    "An opaque external id returned by this API."
                )
                assert "404" in responses


def test_external_contract_exposes_minimal_demographics_and_no_narrative() -> None:
    components = _contract(EXTERNAL_CONTRACT)["components"]
    schemas = components["schemas"]
    assert set(schemas["Patient"]["properties"]) == {"id", "display_name", "birth_date"}
    fields = {
        name for schema in schemas.values() for name in schema.get("properties", {})
    }
    for clinical in ("note", "notes", "soap", "assessment", "plan", "diagnosis"):
        assert clinical not in fields
    assert components["securitySchemes"] == {
        "bearerToken": {"type": "http", "scheme": "bearer"},
        "clientBasic": {"type": "http", "scheme": "basic"},
    }
