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
EARLIEST: Final = datetime(2020, 1, 1, tzinfo=UTC)
LATEST: Final = datetime(2100, 1, 1, tzinfo=UTC)
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
DOCUMENT_KEYS: Final = frozenset({"schema_version", "incidents"})
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


def _text(raw: object, reason: str) -> str:
    # Exact type: a JSON number, boolean, list or object is never a key, so it
    # can never reach a dictionary lookup or a set as an unhashable value.
    if type(raw) is not str:
        _fail(reason)
    return raw


def _record(raw: object, keys: frozenset[str], reason: str) -> dict[str, object]:
    if type(raw) is not dict or set(raw) != keys:
        _fail(reason)
    return raw


def _items(raw: object, reason: str) -> list[object]:
    if type(raw) is not list or not raw:
        _fail(reason)
    return raw


def _timestamp(raw: object) -> datetime:
    text = _text(raw, "update time must be text")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        _fail("update time must be ISO 8601")
    if parsed.tzinfo is None:
        _fail("update time needs an offset")
    try:
        value = parsed.astimezone(UTC)
    except OverflowError:
        _fail("update time is out of range")
    # Bounded so every time also renders in Brasília time without overflow.
    if not EARLIEST <= value < LATEST:
        _fail("update time is out of range")
    return value


def _update(raw: object) -> Update:
    record = _record(raw, UPDATE_KEYS, "update keys are closed")
    state = _text(record["state"], "update state must be text")
    if state not in STATES:
        _fail("unknown update state")
    return Update(_timestamp(record["at"]), state)


def _incident(raw: object) -> Incident:
    record = _record(raw, INCIDENT_KEYS, "incident keys are closed")
    identifier = _text(record["id"], "incident id must be text")
    if not INCIDENT_ID.fullmatch(identifier):
        _fail("incident id must look like INC-2026-0001")
    components = tuple(
        _text(item, "components must be text")
        for item in _items(record["components"], "components must be a list")
    )
    if len(set(components)) != len(components) or not set(components) <= set(
        COMPONENTS
    ):
        _fail("components must be distinct known keys")
    impact = _text(record["impact"], "impact must be text")
    if impact not in IMPACTS:
        _fail("unknown impact")
    parsed = tuple(
        _update(item)
        for item in _items(record["updates"], "an incident needs at least one update")
    )
    if any(later.at <= earlier.at for earlier, later in pairwise(parsed)):
        _fail("updates must be in strictly increasing time order")
    if any(update.state == "resolved" for update in parsed[:-1]):
        _fail("nothing follows a resolved update")
    return Incident(identifier, components, impact, parsed)


def load_incidents(path: Path | None = None) -> tuple[Incident, ...]:
    """Parse and validate every posted incident, newest first.

    Every failure, from unreadable bytes to a value of the wrong JSON type, is
    a ``StatusFileError``; nothing else escapes to the page.
    """
    try:
        document = json.loads((path or STATUS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        _fail("status file is unreadable")
    record = _record(document, DOCUMENT_KEYS, "status file keys are closed")
    # Exact int: JSON true equals 1 in Python and must not pass as version 1.
    if type(record["schema_version"]) is not int or (
        record["schema_version"] != SCHEMA_VERSION
    ):
        _fail("unknown schema version")
    if type(record["incidents"]) is not list:
        _fail("incidents must be a list")
    incidents = tuple(_incident(item) for item in record["incidents"])
    if len({incident.id for incident in incidents}) != len(incidents):
        _fail("incident ids must be unique")
    return tuple(sorted(incidents, key=lambda item: item.updates[0].at, reverse=True))
