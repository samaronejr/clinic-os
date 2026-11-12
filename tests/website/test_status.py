"""Status page: a closed incident vocabulary, newest first, never a false all-clear."""

from __future__ import annotations

import json
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Final

import pytest
from apps.website import status
from apps.website.status import StatusFileError, load_incidents
from django.test import Client
from django.urls import reverse

if TYPE_CHECKING:
    from pathlib import Path

OK: Final = 200
OPEN: Final = {
    "id": "INC-2031-0002",
    "components": ["messaging", "ai"],
    "impact": "partial_outage",
    "updates": [
        {"at": "2031-03-02T12:00:00-03:00", "state": "investigating"},
        {"at": "2031-03-02T12:20:00-03:00", "state": "identified"},
    ],
}
RESOLVED: Final = {
    "id": "INC-2031-0001",
    "components": ["agenda"],
    "impact": "maintenance",
    "updates": [
        {"at": "2031-03-01T22:00:00-03:00", "state": "monitoring"},
        {"at": "2031-03-01T23:00:00-03:00", "state": "resolved"},
    ],
}


class _Marks(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.marks: list[tuple[str, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.marks.extend(item for item in attrs if item[0].startswith("data-"))


def _marks(body: str) -> list[tuple[str, str | None]]:
    parser = _Marks()
    parser.feed(body)
    return parser.marks


def _write(path: Path, incidents: list[object]) -> Path:
    path.write_text(json.dumps({"schema_version": 1, "incidents": incidents}))
    return path


def _page(monkeypatch: pytest.MonkeyPatch, path: Path) -> list[tuple[str, str | None]]:
    monkeypatch.setattr(status, "STATUS_FILE", path)
    response = Client().get(reverse("website:status"))
    assert response.status_code == OK
    return _marks(response.content.decode())


def test_committed_incident_file_is_valid() -> None:
    assert isinstance(load_incidents(), tuple)


def test_empty_file_shows_no_incident_rather_than_operational(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    marks = _page(monkeypatch, _write(tmp_path / "status.json", []))

    assert ("data-state", "clear") in marks
    assert marks.count(("data-status-component", None)) == len(status.COMPONENTS)
    assert ("data-status-none", None) in marks
    assert ("data-status-incident", None) not in marks


def test_open_and_resolved_incidents_render_from_the_closed_vocabulary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = _write(tmp_path / "status.json", [RESOLVED, OPEN])

    incidents = load_incidents(path)
    marks = _page(monkeypatch, path)

    assert [incident.id for incident in incidents] == [OPEN["id"], RESOLVED["id"]]
    assert [incident.resolved for incident in incidents] == [False, True]
    assert ("data-state", "open") in marks
    assert marks.count(("data-status-incident", None)) == 2


@pytest.mark.parametrize(
    "incident",
    [
        {**OPEN, "note": "Paciente Sintetico ligou"},
        {**OPEN, "id": "incident-1"},
        {**OPEN, "components": []},
        {**OPEN, "components": ["agenda", "agenda"]},
        {**OPEN, "components": ["billing-db"]},
        {**OPEN, "impact": "total"},
        {**OPEN, "updates": []},
        {**OPEN, "updates": [{"at": "2031-03-02T12:00:00", "state": "investigating"}]},
        {**OPEN, "updates": [{"at": "2031-03-02T12:00:00Z", "state": "fixed"}]},
        {
            **OPEN,
            "updates": [
                {"at": "2031-03-02T12:00:00Z", "state": "investigating"},
                {"at": "2031-03-02T12:00:00Z", "state": "identified"},
            ],
        },
        {
            **OPEN,
            "updates": [
                {"at": "2031-03-02T12:00:00Z", "state": "resolved"},
                {"at": "2031-03-02T13:00:00Z", "state": "investigating"},
            ],
        },
        {
            **OPEN,
            "updates": [
                {
                    "at": "2031-03-02T12:00:00Z",
                    "state": "investigating",
                    "message": "free text",
                }
            ],
        },
    ],
    ids=[
        "free-text-key",
        "bad-id",
        "no-component",
        "repeated-component",
        "unknown-component",
        "unknown-impact",
        "no-update",
        "naive-time",
        "unknown-state",
        "same-time",
        "after-resolved",
        "update-free-text",
    ],
)
def test_any_deviation_from_the_model_is_refused(
    tmp_path: Path, incident: dict[str, object]
) -> None:
    with pytest.raises(StatusFileError):
        load_incidents(_write(tmp_path / "status.json", [incident]))


def test_duplicate_ids_and_unknown_schema_are_refused(tmp_path: Path) -> None:
    with pytest.raises(StatusFileError):
        load_incidents(_write(tmp_path / "status.json", [OPEN, OPEN]))
    wrong = tmp_path / "wrong.json"
    wrong.write_text(json.dumps({"schema_version": 2, "incidents": []}))
    with pytest.raises(StatusFileError):
        load_incidents(wrong)


def test_invalid_file_says_unavailable_instead_of_all_clear(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    broken = tmp_path / "status.json"
    broken.write_text("{not json")

    marks = _page(monkeypatch, broken)

    assert ("data-state", "unavailable") in marks
    assert ("data-status-unavailable", None) in marks
    assert ("data-status-component", None) not in marks
