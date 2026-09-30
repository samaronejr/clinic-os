"""drf-spectacular hooks that scope the contract to ``/api/ui/v1/``."""

from __future__ import annotations

from typing import Final

UI_API_V1_PREFIX: Final = "/api/ui/v1/"

type Endpoint = tuple[str, str, str, object]


def ui_api_endpoints_only(
    endpoints: list[Endpoint],
    **_kwargs: object,
) -> list[Endpoint]:
    """Keep only UI API v1 operations in the generated schema."""
    return [
        endpoint for endpoint in endpoints if endpoint[0].startswith(UI_API_V1_PREFIX)
    ]
