"""Frozen adapter workload for cost/latency benchmark-v1, not domain evaluation."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import NAMESPACE_URL, uuid5

from apps.ai.brief_contracts import ChartRequest
from apps.ai.contracts import Purpose
from apps.ai.decision_contracts import DecisionRequest
from apps.ai.evaluation.corpus import case_audio
from apps.ai.reference_catalog import load_catalog
from apps.core.chart_sources import ChartSource

if TYPE_CHECKING:
    from apps.ai.contracts import Method
    from apps.ai.evaluation.corpus import Case


def benchmark_input(case: Case, purpose: Purpose) -> tuple[str, Method]:
    """Keep benchmark-v1 inputs independent of the new domain regression protocol."""
    if purpose == Purpose.SCRIBE_ASR:
        return json.dumps(
            {
                "schema": "synthetic-evaluation-audio-v1",
                "encoding": "ogg-opus",
                "audio_base64": base64.b64encode(case_audio(case)).decode(),
            }
        ), "transcribe"
    if purpose in {Purpose.BRIEF, Purpose.DECISION_SUPPORT}:
        identity = uuid5(NAMESPACE_URL, "clinic-os-evaluation:" + case["id"])
        source = ChartSource(
            "observations",
            "observation",
            identity,
            1,
            datetime(2031, 1, 1, tzinfo=UTC),
            case["script"],
        )
        chart = ChartRequest(identity, "1" * 64, "brief", "", (source.reference(),))
        if purpose == Purpose.DECISION_SUPPORT:
            return DecisionRequest(
                identity,
                chart,
                tuple(entry.reference() for entry in load_catalog()),
            ).encode(), "complete"
        return chart.encode(), "complete"
    return case["script"], "extract" if purpose == Purpose.EXTRACTION else "complete"
