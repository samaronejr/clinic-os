r"""drf-spectacular settings that scope the second contract to ``/api/v1/``.

Regenerate with::

    uv run python manage.py spectacular --urlconf apps.interop.api.schema_urls \\
        --custom-settings apps.interop.api.schema.EXTERNAL_API_SETTINGS \\
        --file docs/api/external-v1.yaml --validate --fail-on-warn
"""

from __future__ import annotations

from typing import Final

EXTERNAL_API_V1_PREFIX: Final = "/api/v1/"

type Endpoint = tuple[str, str, str, object]


def external_endpoints_only(
    endpoints: list[Endpoint],
    **_kwargs: object,
) -> list[Endpoint]:
    """Keep only external API v1 operations in the generated schema."""
    return [
        endpoint
        for endpoint in endpoints
        if endpoint[0].startswith(EXTERNAL_API_V1_PREFIX)
    ]


EXTERNAL_API_SETTINGS: Final[dict[str, object]] = {
    "TITLE": "Clinic Ops external API",
    "DESCRIPTION": (
        "Versioned partner API. OAuth 2.0 client credentials issue 15-minute "
        "bearer tokens limited to the client's scopes and clinic. Patient-linked "
        "records use opaque external ids; unknown and foreign ids are the same "
        "404. Every client has a per-minute token bucket (429 with Retry-After). "
        "Writes require an Idempotency-Key. Errors are {code, message_key}; the "
        "token endpoint uses RFC 6749 error bodies."
    ),
    "VERSION": "1",
    "SCHEMA_PATH_PREFIX": r"/api/v1",
    "PREPROCESSING_HOOKS": ["apps.interop.api.schema.external_endpoints_only"],
    "APPEND_COMPONENTS": {
        "securitySchemes": {"clientBasic": {"type": "http", "scheme": "basic"}}
    },
    "ENUM_NAME_OVERRIDES": {},
}
