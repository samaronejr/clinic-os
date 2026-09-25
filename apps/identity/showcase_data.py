"""Inert, synthetic fixtures for the DEBUG-only component showcase.

Every person here is fictional ("Sintético"), identifiers are masked, and no
value is read from the database. Nothing in this module is imported by a
product view.
"""

from __future__ import annotations

from typing import Final

from django.utils.translation import gettext_lazy as _

type Fixture = object

PATIENT_OPTIONS: Final = (
    {
        "value": "p1",
        "label": "Ana Ribeiro Sintética",
        "meta": _("34 years · CPF ***.***.***-41"),
    },
    {
        "value": "p2",
        "label": "Anaí Duarte Sintética",
        "meta": _("8 years · CPF ***.***.***-07"),
    },
    {
        "value": "p3",
        "label": "Anselmo Prado Sintético",
        "meta": _("71 years · CPF ***.***.***-90"),
    },
)
PATIENT_SELECTION: Final = {
    "label": "Ana Ribeiro Sintética",
    "meta": _("34 years · CPF ***.***.***-41"),
}
COMMAND_GROUPS: Final = (
    {
        "label": _("Destinations"),
        "items": (
            {"value": "agenda", "label": _("Agenda"), "meta": _("Today's schedule")},
            {"value": "patients", "label": _("Patients"), "meta": _("Registry")},
        ),
    },
    {
        "label": _("Actions"),
        "items": (
            {
                "value": "book",
                "label": _("Book appointment"),
                "meta": _("Opens the booking form"),
            },
            {
                "value": "arrival",
                "label": _("Record arrival"),
                "meta": _("Reception queue"),
            },
        ),
    },
)
GRID_RESOURCES: Final = ("Dra. Lia Sintética", _("Room 2"), _("Ultrasound"))
GRID_ROWS: Final = (
    {
        "time": "08:00",
        "cells": (
            {"kind": "booked", "title": "Ana R. Sintética", "meta": _("Return visit")},
            {"kind": "free"},
            {"kind": "held", "title": _("Hold"), "meta": _("expires 08:10")},
        ),
    },
    {
        "time": "08:30",
        "cells": (
            {"kind": "done", "title": "Anselmo P. Sintético", "meta": _("Completed")},
            {"kind": "conflict", "title": _("Two bookings"), "meta": _("Resolve")},
            {"kind": "free"},
        ),
    },
    {
        "time": "09:00",
        "cells": (
            {"kind": "free"},
            {"kind": "blocked", "title": _("Unavailable")},
            {"kind": "booked", "title": "Anaí D. Sintética", "meta": _("First visit")},
        ),
    },
)
TABS: Final = (
    {"key": "overview", "label": _("Overview")},
    {"key": "timeline", "label": _("Timeline"), "count": 12},
    {"key": "results", "label": _("Results"), "count": 3},
    {"key": "documents", "label": _("Documents")},
)
TABS_DISABLED: Final = (
    *TABS[:3],
    {"key": "documents", "label": _("Documents (no access)"), "disabled": True},
)
SEGMENTS: Final = (
    {"value": "day", "label": _("Day")},
    {"value": "week", "label": _("Week")},
    {"value": "month", "label": _("Month")},
)
EDITOR_SECTIONS: Final = (
    {
        "key": "subjective",
        "label": _("Subjective"),
        "value": _("Synthetic complaint: headache for three days, no fever."),
    },
    {
        "key": "plan",
        "label": _("Plan"),
        "value": _("Synthetic plan: hydration and review in seven days."),
    },
)
DIFF_LINES: Final = (
    {"kind": "same", "text": _("Headache for three days.")},
    {"kind": "removed", "text": _("No fever reported.")},
    {"kind": "added", "text": _("Low fever reported on day two.")},
    {"kind": "same", "text": _("Review in seven days.")},
)
DOCUMENT_REGIONS: Final = (
    {"id": "r1", "label": _("Hemoglobin"), "text": _("Hemoglobin 13.2 g/dL")},
    {"id": "r2", "label": _("Leukocytes"), "text": _("Leukocytes 6,400 /µL")},
)
CHART_LABELS: Final = ("03/2031", "04/2031", "05/2031", "06/2031", "07/2031")
CHART_SERIES: Final = (
    {
        "name": _("Hemoglobin (g/dL)"),
        "values": ("11.4", "12.1", "12.8", "13.4", "13.2"),
    },
)

FIXTURES: Final[dict[str, Fixture]] = {
    "patient_options": PATIENT_OPTIONS,
    "patient_selection": PATIENT_SELECTION,
    "command_groups": COMMAND_GROUPS,
    "grid_resources": GRID_RESOURCES,
    "grid_rows": GRID_ROWS,
    "tabs": TABS,
    "tabs_disabled": TABS_DISABLED,
    "segments": SEGMENTS,
    "editor_sections": EDITOR_SECTIONS,
    "diff_lines": DIFF_LINES,
    "document_regions": DOCUMENT_REGIONS,
    "chart_labels": CHART_LABELS,
    "chart_series": CHART_SERIES,
}


def showcase_fixture(name: str) -> Fixture:
    """Return one named synthetic fixture; unknown names fail loudly."""
    return FIXTURES[name]
