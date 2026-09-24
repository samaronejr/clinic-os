"""Data-only overlay validation shared by the existing immutable publishers."""

import re

from django.core.exceptions import ValidationError

_CONTROL_CHARACTER_BOUNDARY = 32
_ACTIVE_CONTENT = re.compile(
    r"[<>]|(?:javascript|vbscript|data)\s*:|/\*|\*/|[{}]|"
    r"(?:display|visibility|position|color|background)\s*:",
    re.IGNORECASE,
)


def validate_overlay_text(value: str) -> None:
    """Reject markup, executable URLs, CSS and non-text control characters."""
    if _ACTIVE_CONTENT.search(value) or any(
        ord(character) < _CONTROL_CHARACTER_BOUNDARY and character not in "\r\n\t"
        for character in value
    ):
        msg = "Use somente texto simples, sem HTML, scripts ou CSS."
        raise ValidationError(msg)
