"""Per-user display preferences (theme and density), stored server-side.

Authority comes from the transaction's ``app.current_user_id`` GUC: the
service never takes a user argument, and row security limits every read and
write to that user's own row. Nothing is kept in browser storage.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from django.db.models import CharField, Func, UUIDField, Value
from django.db.models.functions import Cast, NullIf

from apps.identity.current_context import current_actor_id
from apps.identity.models import UserPreference

THEMES: Final = frozenset(UserPreference.Theme.values)
DENSITIES: Final = frozenset(UserPreference.Density.values)


class PreferenceValueError(ValueError):
    """Reject a theme or density outside the closed vocabulary."""

    def __init__(self) -> None:
        """Expose a payload-free message."""
        super().__init__("unsupported display preference")


@dataclass(frozen=True)
class UiPreferences:
    """The two display settings the shell applies to <html>."""

    theme: str
    density: str


DEFAULT_PREFERENCES: Final = UiPreferences(
    theme=UserPreference.Theme.LIGHT.value,
    density=UserPreference.Density.COMFORTABLE.value,
)


def load_ui_preferences() -> UiPreferences:
    """Return the current user's preferences, or the defaults without a row."""
    current_user = Cast(
        NullIf(
            Func(
                Value("app.current_user_id"),
                Value(True),  # noqa: FBT003 - SQL missing_ok argument
                function="pg_catalog.current_setting",
                output_field=CharField(),
            ),
            Value(""),
        ),
        output_field=UUIDField(),
    )
    row = (
        UserPreference.objects.filter(user_id=current_user)
        .values_list("theme", "density")
        .first()
    )
    if row is None:
        return DEFAULT_PREFERENCES
    return UiPreferences(theme=row[0], density=row[1])


def save_ui_preferences(*, theme: str, density: str) -> UiPreferences:
    """Validate and store the current user's preferences (insert or update)."""
    if theme not in THEMES or density not in DENSITIES:
        raise PreferenceValueError
    UserPreference.objects.update_or_create(
        user_id=current_actor_id(),
        defaults={"theme": theme, "density": density},
    )
    return UiPreferences(theme=theme, density=density)
