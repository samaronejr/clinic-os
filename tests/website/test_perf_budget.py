"""The website LCP budget verdict (ops/testing/website_perf.py), without a browser."""

from __future__ import annotations

import pytest
from ops.testing.website_perf import (
    LCP_BUDGET_MS,
    SLOW_4G,
    Measurement,
    PerfBudgetError,
    main,
    verdict,
)


def _measure(lcp_ms: float, status: int = 200) -> Measurement:
    return Measurement(
        path="/",
        status=status,
        lcp_ms=lcp_ms,
        lcp_element="h1",
        document_received_ms=620.0,
        transfer_bytes=80_000,
    )


def test_the_profile_is_lighthouse_mobile_slow_4g() -> None:
    assert SLOW_4G["latency"] == 562.5
    assert SLOW_4G["downloadThroughput"] == pytest.approx(1474.56 * 1024 / 8)
    assert LCP_BUDGET_MS == 2500.0


def test_a_page_passes_only_below_the_budget_with_a_200() -> None:
    assert verdict([_measure(2499.9)])["passed"] is True
    assert verdict([_measure(2500.0)])["passed"] is False
    assert verdict([_measure(900.0), _measure(1200.0, status=404)])["passed"] is False


def test_an_empty_run_fails_instead_of_passing() -> None:
    with pytest.raises(PerfBudgetError, match="no page was measured"):
        verdict([])


def test_the_cli_refuses_a_non_loopback_origin() -> None:
    with pytest.raises(PerfBudgetError, match="loopback"):
        main(
            [
                "--base-url",
                "https://clinic-ops.invalid",
                "--executable",
                "/usr/bin/false",
                "--report",
                "report.json",
            ]
        )
