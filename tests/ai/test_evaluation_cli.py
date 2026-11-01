"""The actual owner commands run outside transactions and export synthetic evidence."""

from __future__ import annotations

import io
import json
import os
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from apps.ai.models import EvaluationRun
from django.core.management import CommandError, call_command
from django.db import connection

from ai.evaluation_support import as_reviewer, complete_reviews, evaluation_world
from otp_test_support import runtime_role

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_owner_cli_runs_registered_corpus_and_exports_adjudicated_report(
    rbac_graph: RbacGraph, tmp_path: Path
) -> None:
    world = evaluation_world(rbac_graph)
    common = [
        "--organization-id",
        str(world.organization),
        "--clinic-id",
        str(world.clinic),
        "--operator-id",
        str(world.owner),
    ]
    selection = ["--capability", "AI-04", "--corpus", "engineering-v1"]
    sink = io.StringIO()
    call_command(
        "ai_eval",
        "register",
        *common,
        *selection,
        "--clinical-owner-ref",
        "synthetic-cli-preregistered",
        stdout=sink,
    )
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_user")
        assert cursor.fetchone() == ("clinic_owner",)
    encoded = call_command(
        "ai_eval",
        "run",
        *common,
        *selection,
        "--configuration-id",
        str(world.configuration.pk),
        "--rater-one",
        str(world.reviewers.rater_one),
        "--rater-two",
        str(world.reviewers.rater_two),
        "--adjudicator",
        str(world.reviewers.adjudicator),
        stdout=sink,
    )
    metadata = json.loads(encoded)
    assert metadata["status"] == "pending"
    with as_reviewer(world, world.owner):
        run = EvaluationRun.objects.get(pk=UUID(metadata["run_id"]))
    reviewed = replace(world, run=run)
    assert complete_reviews(reviewed).status == "passed"
    assert (
        call_command("ai_eval", "finish", *common, "--run-id", str(run.pk), stdout=sink)
        == "passed"
    )
    change = call_command(
        "ai_eval", "request-change", *common, "--run-id", str(run.pk), stdout=sink
    )
    decider = [*common[:-1], str(world.approver)]
    assert (
        call_command(
            "ai_eval",
            "decide",
            *decider,
            "--change-id",
            change,
            "--decision",
            "approved",
            "--approval-ref",
            "synthetic-cli-change",
            stdout=sink,
        )
        == "approved"
    )
    assert call_command(
        "ai_gateway",
        "model-state",
        "--configuration-id",
        str(world.configuration.pk),
        "--state",
        "active",
        stdout=sink,
    ) == str(world.configuration.pk)
    destination = Path(os.environ.get("CLINIC_T47_EVIDENCE_ROOT", str(tmp_path)))
    html = destination / "eval-report.html"
    rendered = call_command(
        "ai_eval",
        "report",
        *common,
        "--run-id",
        str(run.pk),
        "--html",
        str(html),
        stdout=sink,
    )
    report = json.loads(rendered)
    assert report["evaluation_status"] == "passed"
    assert report["real_world_performance"] is False
    assert report["external_gates_satisfied"] == []
    assert html.is_file()
    assert b"<script" not in html.read_bytes()
    (destination / "eval-report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    with pytest.raises(CommandError):
        call_command(
            "ai_eval",
            "register",
            "--organization-id",
            str(world.organization),
            "--clinic-id",
            str(world.clinic),
            "--operator-id",
            str(world.reviewers.rater_one),
            *selection,
            "--clinical-owner-ref",
            "synthetic-not-owner",
            stdout=sink,
        )
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_user")
        assert cursor.fetchone() == ("clinic_owner",)
    with pytest.raises(CommandError):
        call_command(
            "ai_eval",
            "report",
            *common,
            "--run-id",
            "malformed-selector",
            stdout=sink,
        )


def test_owner_cli_redaction_never_carries_corpus_or_selector_text() -> None:
    selector = "SINTETICO-SENTINELA-EVALUATION-SELECTOR"
    clinic, actor = str(uuid4()), str(uuid4())
    with pytest.raises(CommandError) as caught:
        call_command(
            "ai_eval",
            "register",
            "--organization-id",
            selector,
            "--clinic-id",
            clinic,
            "--operator-id",
            actor,
            "--capability",
            "AI-04",
            "--corpus",
            "engineering-v1",
            "--clinical-owner-ref",
            "synthetic-redaction-probe",
            stdout=io.StringIO(),
        )
    rendered = str(caught.value)
    assert all(
        value not in rendered for value in (selector, clinic, actor, "engineering-v1")
    )
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_user")
        assert cursor.fetchone() == ("clinic_owner",)


def test_cli_owner_scope_restores_role_after_service_failure_and_denies_runtime_entry(
    rbac_graph: RbacGraph,
) -> None:
    world = evaluation_world(rbac_graph)
    arguments = (
        "register",
        "--organization-id",
        str(world.organization),
        "--clinic-id",
        str(world.clinic),
        "--operator-id",
        str(world.owner),
        "--capability",
        "AI-04",
        "--corpus",
        "engineering-v1",
        "--clinical-owner-ref",
        "SINTETICO-SENTINELA-INVALID-REFERENCE",
    )
    with pytest.raises(CommandError) as caught:
        call_command("ai_eval", *arguments, stdout=io.StringIO())
    assert "SINTETICO-SENTINELA" not in str(caught.value)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT current_user, "
            "NULLIF(current_setting('app.current_user_id',true),'')"
        )
        assert cursor.fetchone() == ("clinic_owner", None)
    with runtime_role():
        with pytest.raises(CommandError):
            call_command("ai_eval", *arguments, stdout=io.StringIO())
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_user")
            assert cursor.fetchone() == ("clinic_app",)
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_user")
        assert cursor.fetchone() == ("clinic_owner",)
