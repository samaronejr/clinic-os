"""Portal display choices kept in the server-side session, never in the browser.

Patient sessions have no user account to hang preferences on, so the choices
live in the Django session store for the length of the session. They change
only how the portal renders (text size and contrast); no record, authority or
clinical value depends on them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from django.contrib.sessions.backends.base import SessionBase

PORTAL_DISPLAY_KEY: Final = "portal_display"
TEXT_SIZES: Final = ("standard", "large")
CONTRASTS: Final = ("standard", "high")


@dataclass(frozen=True, slots=True)
class PortalDisplay:
    """Server-side display choices; the defaults are the design-system look."""

    text_size: str = "standard"
    contrast: str = "standard"


def display_preferences(session: SessionBase | None) -> PortalDisplay:
    """Read the display choices; anything outside the closed sets is the default."""
    stored = None if session is None else session.get(PORTAL_DISPLAY_KEY)
    if not isinstance(stored, dict):
        return PortalDisplay()
    text_size = stored.get("text_size")
    contrast = stored.get("contrast")
    return PortalDisplay(
        text_size=text_size if text_size in TEXT_SIZES else "standard",
        contrast=contrast if contrast in CONTRASTS else "standard",
    )


def save_display_preferences(
    session: SessionBase, *, text_size: object, contrast: object
) -> PortalDisplay:
    """Store validated display choices server-side; reject unknown values."""
    if text_size not in TEXT_SIZES or contrast not in CONTRASTS:
        raise ValueError
    display = PortalDisplay(text_size=str(text_size), contrast=str(contrast))
    session[PORTAL_DISPLAY_KEY] = {
        "text_size": display.text_size,
        "contrast": display.contrast,
    }
    return display
