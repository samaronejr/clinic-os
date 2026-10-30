"""Human-adjudicated descriptive reports, with denominators and encounter CIs."""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING, cast

from django.template.loader import render_to_string
from django.utils.translation import gettext as _

from apps.ai.evaluation.forms import LABELS
from apps.ai.evaluation.metrics import (
    cluster_bootstrap,
    cohens_kappa,
    wilson_interval,
)
from apps.ai.evaluation.protocol import (
    CLINICAL_LABELS,
    ERROR_LABELS,
    EvaluationInputError,
    category,
    validate_labels,
)
from apps.ai.evaluation.services import (
    authorized_run,
    evaluation_staff,
    thresholds_for,
)
from apps.ai.models import EvaluationRating

if TYPE_CHECKING:
    from uuid import UUID


def build_report(*, clinic_id: UUID, run_id: UUID) -> dict[str, object]:
    """Never turn incomplete votes or a synthetic sample into a safety claim."""
    evaluation_staff(clinic_id=clinic_id, mode="clinical_owner")
    run = authorized_run(clinic_id=clinic_id, run_id=run_id)
    if run.status == "pending" or not isinstance(run.sample_results, dict):
        raise EvaluationInputError
    ratings = {
        (row.case_id, row.slot): validate_labels(row.labels)
        for row in EvaluationRating.objects.filter(run=run)
    }
    thresholds = thresholds_for(run)
    cases = cast("list[dict[str, object]]", run.sample_results["cases"])
    counts = dict.fromkeys(ERROR_LABELS, 0)
    first: list[str] = []
    second: list[str] = []
    errors: dict[str, list[float]] = defaultdict(list)
    correction: dict[str, list[float]] = defaultdict(list)
    strata: dict[str, dict[str, list[int]]] = {
        axis: defaultdict(list) for axis in ("specialty", "device", "accent")
    }
    reference_words = word_errors = adjudications = 0
    for case in cases:
        key = str(case["id"])
        one, two = ratings[key, "rater_one"], ratings[key, "rater_two"]
        first.append(category(one))
        second.append(category(two))
        if category(one) == category(two):
            labels = one
        else:
            labels = ratings[key, "adjudicator"]
            adjudications += 1
        for label in ERROR_LABELS:
            counts[label] += labels[label]
        significant = int(any(labels[label] for label in CLINICAL_LABELS))
        encounter = str(case["encounter"])
        errors[encounter].append(significant)
        correction[encounter].append((one["correction_ms"] + two["correction_ms"]) / 2)
        for axis, groups in strata.items():
            groups[str(case[axis])].append(significant)
        words = cast("dict[str, int]", case["word_errors"])
        reference_words += words["reference_words"]
        word_errors += words["errors"]
    total = len(cases)
    clinical_errors = sum(sum(values) for values in errors.values())
    rates = {
        name: {
            "numerator": numerator,
            "denominator": total,
            "rate": numerator / total,
            "wilson_95": wilson_interval(numerator, total),
        }
        for name, numerator in {
            **counts,
            "clinical_error": int(clinical_errors),
            "citation_support": total - counts["unsupported_citation"],
            "action_correctness": total - counts["incorrect_action"],
        }.items()
    }
    bootstrap_options = {
        "seed": thresholds["bootstrap_seed"],
        "iterations": thresholds["bootstrap_iterations"],
    }
    return {
        "schema_version": 1,
        "data_class": "synthetic",
        "real_world_performance": False,
        "external_gates_satisfied": [],
        "evaluation_status": run.status,
        "capability": run.protocol.capability,
        "configuration_version": run.configuration_version,
        "versions": run.versions,
        "corpus_version": run.protocol.corpus_version,
        "corpus_sha256": run.protocol.corpus_sha256,
        "thresholds_sha256": run.protocol.thresholds_sha256,
        "thresholds": thresholds,
        "registered_at": run.protocol.created_at.isoformat(),
        "scored_at": run.scored_at.isoformat() if run.scored_at else None,
        "cases": total,
        "encounters": len(errors),
        "human_review_complete": True,
        "adjudicated_disagreements": adjudications,
        "kappa": cohens_kappa(first, second),
        "rates": rates,
        "clinical_error_cluster_bootstrap_95": cluster_bootstrap(
            errors, **bootstrap_options
        ),
        "correction_ms": {
            "basis": "mean_of_two_independent_raters",
            "mean": sum(sum(values) for values in correction.values()) / total,
            "cluster_bootstrap_95": cluster_bootstrap(correction, **bootstrap_options),
        },
        "word_error_rate": {
            "errors": word_errors,
            "reference_words": reference_words,
            "rate": word_errors / reference_words if reference_words else None,
            "gating_alone": False,
        },
        "strata": {
            axis: [
                {
                    "tag": tag,
                    "errors": sum(values),
                    "cases": len(values),
                    "rate": sum(values) / len(values),
                    "wilson_95": wilson_interval(sum(values), len(values)),
                }
                for tag, values in sorted(groups.items())
            ]
            for axis, groups in strata.items()
        },
        "limitations": _(
            "Synthetic engineering fixtures and scripted review are not representative "
            "clinical validation. Zero observed critical errors is a gate, not a "
            "safety "
            "claim. Real-world performance and clinical thresholds require EG-3/EG-11. "
            "AI-05/AI-06 fixture contracts do not implement their future workflows."
        ),
    }


def report_html(report: dict[str, object]) -> str:
    """Render a self-contained inert report, with no scripts or external resources."""
    return render_to_string(
        "ai/evaluation_report.html",
        {
            "report": report,
            "status": LABELS[str(report["evaluation_status"])],
            "rates": [
                (LABELS[name], metric)
                for name, metric in cast("dict[str, object]", report["rates"]).items()
            ],
            "strata": [
                (LABELS[axis], groups)
                for axis, groups in cast("dict[str, object]", report["strata"]).items()
            ],
            "undefined": _("Undefined"),
        },
    )
