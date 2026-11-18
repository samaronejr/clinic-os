"""Run-wide display default for the renewal runtime's signed-in pages.

The browser runner chooses one theme and density per run (``--theme``,
``--density``). ``config.settings.renewal`` resolves them here into the server's
display default, the value ``load_ui_preferences`` returns for a user with no
stored ``UserPreference`` row; ``templates/base.html`` then renders it as
``data-theme``/``data-density`` on signed-in pages, the app's own mechanism. A
stored row still wins, and public pages carry neither attribute, so they stay
light (DESIGN.md). Base, production and test settings never read these
variables. Pure, so the runner resolves the same value it hands the server.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Mapping

THEMES: Final = ("light", "dark")
DENSITIES: Final = ("comfortable", "compact")
DEFAULT_THEME: Final = THEMES[0]
DEFAULT_DENSITY: Final = DENSITIES[0]
THEME_VARIABLE: Final = "CLINIC_RENEWAL_THEME"
DENSITY_VARIABLE: Final = "CLINIC_RENEWAL_DENSITY"


class DisplayValueError(ValueError):
    """Reject a theme or density outside the closed vocabulary."""

    def __init__(self, name: str, value: str) -> None:
        """Name the axis and the refused value."""
        super().__init__(f"unsupported renewal display {name}: {value!r}")


def resolve_display(environ: Mapping[str, str]) -> tuple[str, str]:
    """Return ``(theme, density)``, light and comfortable unless chosen."""
    theme = environ.get(THEME_VARIABLE, DEFAULT_THEME)
    density = environ.get(DENSITY_VARIABLE, DEFAULT_DENSITY)
    for axis, value, allowed in (
        ("theme", theme, THEMES),
        ("density", density, DENSITIES),
    ):
        if value not in allowed:
            raise DisplayValueError(axis, value)
    return theme, density
