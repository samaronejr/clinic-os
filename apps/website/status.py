"""Status page content model: manually posted incidents from a closed vocabulary.

Operators record incidents in ``docs/operations/status-incidents.json``. An
incident names components, an impact and dated updates, each with a state; all
of them are keys of the fixed vocabularies below, and every sentence the page
shows is a translated msgid chosen by state. There is no free-text field, so a
status post cannot carry a customer name, a patient detail or an error
message. ``load_incidents`` validates the whole file and refuses it on the
first problem; the page then says status is unavailable instead of showing
a partial record or an all-clear it cannot back.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Final, Never

# Stored as English msgids and translated when the page renders.
from django.utils.translation import gettext_noop as _

STATUS_FILE: Final = (
    Path(__file__).resolve().parents[2] / "docs/operations/status-incidents.json"
)
SCHEMA_VERSION: Final = 1
INCIDENT_ID: Final = re.compile(r"^INC-[0-9]{4}-[0-9]{4}$")
COMPONENTS: Final[dict[str, str]] = {
    "agenda": _("Agenda and booking"),
    "records": _("Clinical records"),
    "portal": _("Patient portal"),
    "messaging": _("Messages and reminders"),
    "payments": _("Payments"),
    "video": _("Teleconsultation video"),
    "ai": _("AI assistance"),
}
# Ordered from least to most severe; the component row shows the worst open one.
IMPACTS: Final[dict[str, str]] = {
    "maintenance": _("Planned maintenance"),
    "degraded": _("Degraded performance"),
    "partial_outage": _("Partial outage"),
    "major_outage": _("Major outage"),
}
STATES: Final[dict[str, str]] = {
    "investigating": _("Investigating"),
    "identified": _("Cause identified"),
    "monitoring": _("Monitoring the fix"),
    "resolved": _("Resolved"),
}
MESSAGES: Final[dict[str, str]] = {
    "investigating": _("A problem was reported and is being investigated."),
    "identified": _("The cause was found and a fix is in progress."),
    "monitoring": _("A fix is in place and the results are being watched."),
    "resolved": _("This incident is resolved."),
}
INCIDENT_KEYS: Final = frozenset({"id", "components", "impact", "updates"})
UPDATE_KEYS: Final = frozenset({"at", "state"})


class StatusFileError(ValueError):
    """The incident file does not match the closed content model."""


@dataclass(frozen=True, slots=True)
class Update:
    """One dated state change of an incident."""

    at: datetime
    state: str


@dataclass(frozen=True, slots=True)
class Incident:
    """A posted incident; its state is the state of its latest update."""

    id: str
    components: tuple[str, ...]
    impact: str
    updates: tuple[Update, ...]

    @property
    def state(self) -> str:
        """Return the state of the latest update."""
        return self.updates[-1].state

    @property
    def resolved(self) -> bool:
        """Report whether the latest update resolved the incident."""
        return self.state == "resolved"


def _fail(reason: str) -> Never:
    raise StatusFileError(reason)


def _timestamp(raw: object) -> datetime:
    if not isinstance(raw, str):
        _fail("update time must be text")
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        _fail("update time must be ISO 8601")
    if value.tzinfo is None:
        _fail("update time needs an offset")
    return value.astimezone(UTC)


def _update(raw: object) -> Update:
    if not isinstance(raw, dict) or set(raw) != UPDATE_KEYS:
        _fail("update keys are closed")
    if raw["state"] not in STATES:
        _fail("unknown update state")
    return Update(_timestamp(raw["at"]), raw["state"])


def _incident(raw: object) -> Incident:
    if not isinstance(raw, dict) or set(raw) != INCIDENT_KEYS:
        _fail("incident keys are closed")
    identifier, components, impact, updates = (
        raw["id"],
        raw["components"],
        raw["impact"],
        raw["updates"],
    )
    if not isinstance(identifier, str) or not INCIDENT_ID.match(identifier):
        _fail("incident id must look like INC-2026-0001")
    if (
        not isinstance(components, list)
        or not components
        or len(set(components)) != len(components)
        or not set(components) <= set(COMPONENTS)
    ):
        _fail("components must be distinct known keys")
    if impact not in IMPACTS:
        _fail("unknown impact")
    if not isinstance(updates, list) or not updates:
        _fail("an incident needs at least one update")
    parsed = tuple(_update(item) for item in updates)
    if any(later.at <= earlier.at for earlier, later in pairwise(parsed)):
        _fail("updates must be in strictly increasing time order")
    if any(update.state == "resolved" for update in parsed[:-1]):
        _fail("nothing follows a resolved update")
    return Incident(identifier, tuple(components), impact, parsed)


def load_incidents(path: Path | None = None) -> tuple[Incident, ...]:
    """Parse and validate every posted incident, newest first."""
    try:
        document = json.loads((path or STATUS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _fail("status file is unreadable")
    if (
        not isinstance(document, dict)
        or set(document) != {"schema_version", "incidents"}
        or document["schema_version"] != SCHEMA_VERSION
        or not isinstance(document["incidents"], list)
    ):
        _fail("status file keys are closed")
    incidents = tuple(_incident(item) for item in document["incidents"])
    if len({incident.id for incident in incidents}) != len(incidents):
        _fail("incident ids must be unique")
    return tuple(sorted(incidents, key=lambda item: item.updates[0].at, reverse=True))
