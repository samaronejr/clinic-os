"""Prove that question classification cannot observe staff authority or data."""

from apps.analytics.analyst_contracts import question_metric
from apps.analytics.contracts import AnalyticsAccessDeniedError

from identity.nonstaff_differential import DifferentialProbe


def _cross_clinic() -> bool:
    try:
        question_metric("show all clinics revenue")
    except AnalyticsAccessDeniedError:
        return False
    return True


def analytics_probes() -> list[DifferentialProbe]:
    return [
        DifferentialProbe(
            "apps.analytics.analyst_contracts.question_metric",
            lambda: question_metric("faltas"),
            decision=lambda value: value == "no_show",
        ),
        DifferentialProbe(
            "apps.analytics.analyst_contracts.question_metric",
            _cross_clinic,
            expected=False,
        ),
    ]
