"""All provider protocol methods refuse exfiltration before doing any work."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from apps.ai.adapters import RecordedFixtureProvider, SyntheticProvider
from apps.ai.contracts import ProviderUnavailableError
from apps.ai.output_validation import call_provider

from ai.test_units import request

if TYPE_CHECKING:
    from apps.ai.contracts import Method

ENDPOINTS = (
    "https://exfil.synthetic.invalid/v1/inference",
    "https://ai.synthetic.invalid.exfil.synthetic.invalid/v1/inference",
    "https://ai.synthetic.invalid@exfil.synthetic.invalid/v1/inference",
    "https://ai.synthetic.invalid:443/v1/inference",
    "https://127.0.0.1/v1/inference",
    "https://169.254.169.254/v1/inference",
    "https://[::1]/v1/inference",
    "http://ai.synthetic.invalid/v1/inference",
    "https://ai.synthetic.invalid/v1/inference?redirect=https://exfil.synthetic.invalid",
    "https://ai.synthetic.invalid/v1/inference#https://exfil.synthetic.invalid",
    "https://ai.synthetic.invalid/v1/inference/../export",
    "https://[invalid",
)


@pytest.mark.parametrize("method", ["complete", "stream", "transcribe", "extract"])
@pytest.mark.parametrize("recorded", [False, True], ids=["synthetic", "recorded"])
@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_non_allowlisted_egress_never_reaches_a_network_operation(
    method: Method, recorded: bool, endpoint: str
) -> None:
    provider = RecordedFixtureProvider() if recorded else SyntheticProvider()
    query = replace(request(), endpoint=endpoint)
    with (
        patch(
            "socket.create_connection", side_effect=AssertionError("network attempted")
        ) as network,
        pytest.raises(ProviderUnavailableError),
    ):
        call_provider(provider, request=query, method=method)
    network.assert_not_called()
    assert provider.real_enabled is False


@pytest.mark.parametrize("method", ["complete", "stream", "transcribe", "extract"])
@pytest.mark.parametrize("recorded", [False, True], ids=["synthetic", "recorded"])
def test_exact_synthetic_allowlist_is_a_live_positive_control(
    method: Method, recorded: bool
) -> None:
    provider = RecordedFixtureProvider() if recorded else SyntheticProvider()
    endpoint = (
        "https://recorded.synthetic.invalid/v1/inference"
        if recorded
        else "https://ai.synthetic.invalid/v1/inference"
    )
    with patch(
        "socket.create_connection", side_effect=AssertionError("network attempted")
    ) as network:
        result = call_provider(
            provider, request=replace(request(), endpoint=endpoint), method=method
        )
    assert result.content
    network.assert_not_called()
    assert provider.real_enabled is False
