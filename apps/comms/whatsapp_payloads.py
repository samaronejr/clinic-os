"""Outbound WhatsApp Cloud API request bodies; pure builders, no network.

Shapes follow the Cloud API ``POST /{phone-number-id}/messages`` reference.
Free text (``type: text``) is valid only inside an open customer service window;
reaching out requires ``type: template`` with an approved template name and
language. These builders never open a connection: a live transport is blocked on
EG-1 (provider selection and owner approval) and is deliberately absent.
"""

from __future__ import annotations

from typing import Any, Final

API_VERSION_PIN: Final = "unpinned-until-eg-1"
MAX_TEMPLATE_PARAMETERS: Final = 10
MAX_CHALLENGE_CHARS: Final = 64


def text_payload(*, to: str, body: str) -> dict[str, Any]:
    """Build a free-text message; the caller has already proven the window is open."""
    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to.lstrip("+"),
        "type": "text",
        "text": {"preview_url": False, "body": body},
    }


def template_payload(
    *, to: str, name: str, language: str, parameters: tuple[str, ...]
) -> dict[str, Any]:
    """Build a template message with positional body parameters."""
    if len(parameters) > MAX_TEMPLATE_PARAMETERS:
        message = "too many template parameters"
        raise ValueError(message)
    components: list[dict[str, Any]] = []
    if parameters:
        components.append(
            {
                "type": "body",
                "parameters": [{"type": "text", "text": item} for item in parameters],
            }
        )
    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to.lstrip("+"),
        "type": "template",
        "template": {
            "name": name,
            "language": {"code": language},
            "components": components,
        },
    }


def verification_response(
    *, mode: str, token: str, challenge: str, expected_token: str
) -> str | None:
    """Answer Meta's GET subscription handshake, or None when it must be refused."""
    import hmac  # noqa: PLC0415 - narrow, single-use import

    if (
        mode != "subscribe"
        or not expected_token
        or not hmac.compare_digest(token.encode(), expected_token.encode())
        or not challenge.isascii()
        or not challenge.isalnum()
        or len(challenge) > MAX_CHALLENGE_CHARS
    ):
        return None
    return challenge
