"""Todo 72 fills todo 65's recovery-exercise slot from the recorded exercise."""

from __future__ import annotations

import json
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Any

import pytest
from apps.identity import recovery_exercise
from apps.identity.recovery_exercise import RecoveryExercise, latest_recovery_exercise
from django.template.loader import render_to_string
from django.test import override_settings
from django.utils import translation
from django.utils.translation import gettext

from identity.enterprise_support import enterprise_setup
from patient_service_support import runtime_role
from resilience.test_restore_drill import report

if TYPE_CHECKING:
    from pathlib import Path

    from ops.testing.isolation_common import JsonObject

    from rbac_fixtures import RbacGraph

__all__ = ("report",)
LIMITS = (
    "Synthetic local evidence only: a logical restore, with provider "
    "point-in-time recovery and encrypted provider backup not verified. This "
    "screen does not approve recovery or live operation."
)


class _Slot(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.states: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if "data-recovery-exercise" in values:
            self.states.append(values["data-recovery-exercise"] or "")


def slot_states(html: str) -> list[str]:
    parser = _Slot()
    parser.feed(html)
    return parser.states


def write_record(path: Path, record: object) -> Path:
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def test_a_valid_record_is_summarized_without_claiming_more(
    tmp_path: Path, report: JsonObject
) -> None:
    summary = latest_recovery_exercise(write_record(tmp_path / "record.json", report))
    objectives: Any = report["drill"]
    objectives = objectives["recovery_objectives"]
    assert summary == RecoveryExercise(
        "recorded",
        finished_on="2026-11-11",
        revision="0" * 12,
        rpo_seconds=objectives["rpo_measured_seconds"],
        rpo_proposal_seconds=300,
        rto_seconds=objectives["rto_measured_seconds"],
        rto_proposal_seconds=3600,
        attachment_objects=2,
        audit_events=12,
        tenant_key_versions=2,
    )


def test_an_absent_or_invalid_record_never_shows_a_result(
    tmp_path: Path, report: JsonObject
) -> None:
    assert latest_recovery_exercise(tmp_path / "absent.json").state == "absent"
    assert latest_recovery_exercise(tmp_path).state == "unreadable"
    broken = tmp_path / "broken.json"
    broken.write_text("{", encoding="utf-8")
    assert latest_recovery_exercise(broken).state == "unreadable"
    drill: Any = report["drill"]
    claimed = {**report, "drill": {**drill, "provider_pitr_verified": True}}
    assert (
        latest_recovery_exercise(write_record(tmp_path / "pitr.json", claimed)).state
        == "unreadable"
    )


@pytest.mark.parametrize("state", ["recorded", "absent", "unreadable"])
def test_the_slot_always_states_its_limits_in_portuguese(state: str) -> None:
    exercise = RecoveryExercise(
        state,  # type: ignore[arg-type]
        finished_on="2026-11-11",
        revision="abcdef012345",
        rpo_seconds=3.25,
        rpo_proposal_seconds=300,
        rto_seconds=41.75,
        rto_proposal_seconds=3600,
        attachment_objects=2,
        audit_events=66,
        tenant_key_versions=2,
    )
    with translation.override("pt-br"):
        html = render_to_string(
            "identity/includes/recovery_exercise.html",
            {"recovery_exercise": exercise},
        )
        limits = gettext(LIMITS)
        empty = gettext("No recovery exercise has been recorded yet.")
        unreadable = gettext(
            "The recovery exercise record could not be read. No result is shown."
        )
    assert limits != LIMITS
    assert slot_states(html) == [state]
    assert limits in html
    assert (empty in html) is (state == "absent")
    assert (unreadable in html) is (state == "unreadable")
    assert ("abcdef012345" in html) is (state == "recorded")
    assert ("3,2 s" in html or "3,3 s" in html) is (state == "recorded")


@override_settings(CLINIC_ENTERPRISE_SYNTHETIC=True)
@pytest.mark.django_db(transaction=True)
def test_the_governance_page_shows_the_recorded_exercise(
    rbac_graph: RbacGraph,
    tmp_path: Path,
    report: JsonObject,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = enterprise_setup(rbac_graph)
    url = f"/clinics/{rbac_graph.clinic_a}/settings/governance/"
    monkeypatch.setattr(recovery_exercise, "RECORD_PATH", tmp_path / "absent.json")
    with runtime_role():
        before = setup.client.get(url)
    monkeypatch.setattr(
        recovery_exercise,
        "RECORD_PATH",
        write_record(tmp_path / "record.json", report),
    )
    with runtime_role():
        after = setup.client.get(url)
    assert (before.status_code, after.status_code) == (200, 200)
    assert slot_states(before.content.decode()) == ["absent"]
    assert slot_states(after.content.decode()) == ["recorded"]
    assert after.context["recovery_exercise"].revision == "0" * 12
