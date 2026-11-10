"""Closed insurance metadata events, never claim or patient content."""

from typing import Final

EVENTS: Final = {
    "payer": ("created",),
    "contract": ("created",),
    "tuss": ("imported",),
    "prices": ("created",),
    "membership": ("created",),
    "registration": ("payer_linked",),
    "coverage": ("checked",),
    "guide": ("draft", "validated", "sealed"),
    "batch": (
        "draft",
        "validated",
        "sealed",
        "submitting",
        "submitted",
        "unknown",
        "acknowledged",
        "returned",
    ),
    "submission": ("prepared", "exported"),
    "return": ("ingested",),
    "appeal": ("draft", "clinical_approved", "approved", "submitted"),
    "payment": ("reconciled",),
    "request": ("recorded",),
}
