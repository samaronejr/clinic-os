"""Closed import audit vocabulary; payloads never carry file or patient content."""

from typing import Final

EVENTS: Final = {
    "job": (
        "uploaded",
        "profiled",
        "mapped",
        "dry_run",
        "approved",
        "committed",
        "rolled_back",
    ),
    "mapping": ("saved",),
    "batch": ("committed",),
    "report": ("exported",),
    "records": ("viewed",),
}
