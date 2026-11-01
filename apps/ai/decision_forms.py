"""One native POST contract; authority never comes from form fields."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django import forms
from django.utils.translation import gettext_lazy as _

if TYPE_CHECKING:
    from typing import Any


class DecisionSupportForm(forms.Form):
    """An explicit per-use choice defaults to off, including in another tab."""

    encounter_id = forms.UUIDField()
    invocation_id = forms.UUIDField(required=False)
    command = forms.ChoiceField(
        choices=tuple(
            (command, command)
            for command in (
                "open",
                "generate",
                "display",
                "accepted",
                "modified",
                "rejected",
            )
        )
    )
    opt_in = forms.BooleanField(required=False)

    def clean(self) -> dict[str, Any]:
        """Bind selectors to commands and refuse ambiguous opt-in encodings."""
        data = super().clean()
        if data is None:
            return {}
        command = data.get("command")
        if (
            ("opt_in" in self.data and self.data["opt_in"] != "yes")
            or (command != "generate" and data.get("opt_in"))
            or (
                command in {"display", "accepted", "modified", "rejected"}
                and data.get("invocation_id") is None
            )
            or (
                command in {"open", "generate"}
                and data.get("invocation_id") is not None
            )
        ):
            raise forms.ValidationError(_("Invalid decision-support request."))
        return data
