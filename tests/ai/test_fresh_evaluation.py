"""Reproducible engineering reports; scripted labels satisfy no clinical gate."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from apps.ai.contracts import Purpose
from apps.ai.evaluation.protocol import ENGINEERING_THRESHOLDS, threshold_hash
from apps.ai.evaluation.report import build_report, report_html
from apps.ai.evaluation.services import PURPOSE_CAPABILITIES
from apps.ai.evaluation.versions import candidate_versions

from ai.evaluation_support import (
    as_reviewer,
    candidate,
    complete_reviews,
    review_world,
)

if TYPE_CHECKING:
    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("purpose", list(Purpose))
def test_fresh_engineering_report_binds_current_source_and_preregistered_thresholds(
    rbac_graph: RbacGraph, purpose: Purpose
) -> None:
    started = datetime.now(UTC)
    configuration = candidate(purpose=purpose)
    world = review_world(
        organization=rbac_graph.organization_a,
        clinic=rbac_graph.clinic_a,
        configuration=configuration,
    )
    assert complete_reviews(world).status == "passed"
    with as_reviewer(world, world.owner):
        report = build_report(clinic_id=world.clinic, run_id=world.run.pk)
    assert report["versions"] == candidate_versions(configuration)
    assert report["capability"] == PURPOSE_CAPABILITIES[purpose.value]
    assert report["thresholds_sha256"] == threshold_hash(ENGINEERING_THRESHOLDS)
    assert report["real_world_performance"] is False
    assert report["external_gates_satisfied"] == []
    assert report["human_review_complete"] is False
    assert report["vote_workflow_complete"] is True
    assert report["review_kind"] == "scripted_engineering"
    assert report["run_id"] == str(world.run.pk)
    assert report["domain_protocol_sha256"] == world.protocol.domain_protocol_sha256
    assert report["rollout_stage"] == "offline"
    destination = os.environ.get("CPI80_FRESH_EVAL_DIR")
    if destination is None:
        return
    source_commit = os.environ["CPI80_SOURCE_COMMIT"]
    threshold_commit = os.environ["CPI80_THRESHOLD_COMMIT"]
    committed_at = datetime.fromisoformat(os.environ["CPI80_THRESHOLD_COMMITTED_AT"])
    assert len(source_commit) == len(threshold_commit) == 40
    assert committed_at < started
    packet = {
        "source_commit": source_commit,
        "threshold_registration_commit": threshold_commit,
        "threshold_registration_committed_at": committed_at.isoformat(),
        "threshold_registration_path": (
            "apps/ai/evaluation/engineering-registration.json"
        ),
        "run_started_at": started.isoformat(),
        "run_finished_at": datetime.now(UTC).isoformat(),
        "provider": "synthetic",
        "model_id": "synthetic-v1",
        "purpose": purpose.value,
        "review_kind": "scripted_engineering",
        "clinical_owner_registration": "PENDING-EXTERNAL EG-3",
        "lawful_representative_corpus": "PENDING-EXTERNAL EG-11",
        "report": report,
    }
    root = Path(destination)
    root.mkdir(parents=True, exist_ok=True)
    stem = f"{report['capability']}-{purpose.value}"
    (root / f"{stem}.json").write_text(
        json.dumps(packet, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (root / f"{stem}.html").write_text(report_html(report), encoding="utf-8")
