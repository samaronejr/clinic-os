"""Stable resource-booking codes and metadata-only translated refusals."""

from typing import ClassVar

from django.utils.functional import Promise
from django.utils.translation import gettext_lazy as _


class SchedulingRuleError(Exception):
    """Expose a machine code without naming an occupied resource or patient."""

    MESSAGES: ClassVar[dict[str, Promise]] = {
        "resource_conflict": _("A required resource is unavailable for this window."),
        "outside_template": _(
            "Choose a window inside the resource availability template."
        ),
        "holiday": _("This window overlaps a holiday or absence."),
        "buffer_violation": _(
            "The service duration and buffers must fit the available window."
        ),
    }

    def __init__(self, code: str) -> None:
        """Keep identifiers and user content out of exceptions."""
        self.code = code
        self.message = self.MESSAGES[code]
        super().__init__(code)
