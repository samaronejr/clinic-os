"""The resilience suite supplies the same closed fault mode to every boundary."""

from __future__ import annotations

import pytest
from ops.testing import renewal_runner


@pytest.mark.parametrize("mode", ["down", "slow", "ambiguous"])
def test_resilience_runner_faults_every_provider(
    mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLINIC_RENEWAL_RESILIENCE_MODE", mode)
    environment = renewal_runner._synthetic_adapters("resilience")
    assert environment["CLINIC_SYNTHETIC_FAULTS"].split(",") == [
        f"{family}={mode}"
        for family in ("ai", "video", "messaging", "payment", "signing")
    ]
    assert environment["CLINIC_RENEWAL_RESILIENCE_MODE"] == mode
    assert environment["CLINIC_ENTERPRISE_SYNTHETIC"] == "true"


@pytest.mark.parametrize("mode", ["", "live", "timeout", "down,signing=slow"])
def test_resilience_runner_refuses_unknown_modes(
    mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLINIC_RENEWAL_RESILIENCE_MODE", mode)
    with pytest.raises(
        renewal_runner.RenewalRunnerError, match="invalid resilience fault mode"
    ):
        renewal_runner._synthetic_adapters("resilience")
