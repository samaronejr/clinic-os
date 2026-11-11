"""Todo 72: the settings-driven synthetic fault contract; no database needed."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from apps.ai.adapters import RecordedFixtureProvider, SyntheticProvider
from apps.ai.contracts import (
    GatewayInputError,
    ProviderRequest,
    ProviderTimeoutError,
    ProviderUnavailableError,
    Usage,
)
from apps.ai.output_validation import call_provider
from apps.core.fault_injection import injected_fault
from config.settings.contracts import (
    SYNTHETIC_FAULT_FAMILIES,
    SYNTHETIC_FAULT_MODES,
    resolve_synthetic_faults,
)
from django.core.exceptions import ImproperlyConfigured
from ops.release.activation import SYNTHETIC_ONLY_ENV_VARS, live_environment_findings

if TYPE_CHECKING:
    from pytest_django.fixtures import SettingsWrapper


def _request(host: str = "ai.synthetic.invalid") -> ProviderRequest:
    return ProviderRequest(
        "synthetic-v1",
        f"https://{host}/v1/inference",
        "prompt-v1",
        "Synthetic encrypted input reference.",
        Usage(tokens_in=10, tokens_out=10, audio_seconds=5),
        5,
    )


def test_an_empty_setting_injects_nothing() -> None:
    assert resolve_synthetic_faults({}, "synthetic") == {}
    assert resolve_synthetic_faults({"CLINIC_SYNTHETIC_FAULTS": "  "}, "live") == {}


def test_pairs_parse_into_one_mode_per_family() -> None:
    raw = " ai=slow , video=down,messaging=ambiguous,signing=down,payment=slow "
    assert resolve_synthetic_faults({"CLINIC_SYNTHETIC_FAULTS": raw}, "synthetic") == {
        "ai": "slow",
        "video": "down",
        "messaging": "ambiguous",
        "signing": "down",
        "payment": "slow",
    }
    assert SYNTHETIC_FAULT_FAMILIES == (
        "ai",
        "video",
        "messaging",
        "signing",
        "payment",
    )
    assert SYNTHETIC_FAULT_MODES == ("down", "slow", "ambiguous")


@pytest.mark.parametrize(
    "raw",
    [
        "ai",
        "ai=",
        "ai=offline",
        "storage=down",
        "AI=down",
        "ai=down,ai=slow",
        "ai=down,,video=down",
        "ai:down",
    ],
)
def test_malformed_faults_fail_closed_at_startup(raw: str) -> None:
    with pytest.raises(ImproperlyConfigured, match="closed fault vocabulary"):
        resolve_synthetic_faults({"CLINIC_SYNTHETIC_FAULTS": raw}, "synthetic")


def test_live_mode_refuses_any_fault_at_startup_and_activation() -> None:
    with pytest.raises(ImproperlyConfigured, match="only in synthetic data mode"):
        resolve_synthetic_faults({"CLINIC_SYNTHETIC_FAULTS": "ai=down"}, "live")
    assert "CLINIC_SYNTHETIC_FAULTS" in SYNTHETIC_ONLY_ENV_VARS
    findings = live_environment_findings({"CLINIC_SYNTHETIC_FAULTS": "payment=down"})
    assert "CLINIC_SYNTHETIC_FAULTS is synthetic-only and must be unset" in findings
    assert "CLINIC_SYNTHETIC_FAULTS is synthetic-only and must be unset" not in (
        live_environment_findings({})
    )


def test_runtime_lookup_returns_only_configured_closed_modes(
    settings: SettingsWrapper,
) -> None:
    settings.CLINIC_SYNTHETIC_FAULTS = {}
    assert injected_fault("ai") is None
    settings.CLINIC_SYNTHETIC_FAULTS = {"video": "ambiguous"}
    assert injected_fault("video") == "ambiguous"
    assert injected_fault("ai") is None
    settings.CLINIC_SYNTHETIC_FAULTS = {"ai": "offline"}
    with pytest.raises(ImproperlyConfigured, match="closed vocabulary"):
        injected_fault("ai")
    settings.CLINIC_SYNTHETIC_FAULTS = ("ai", "down")
    with pytest.raises(ImproperlyConfigured, match="synthetic data mode"):
        injected_fault("ai")
    settings.CLINIC_SYNTHETIC_FAULTS = {"ai": "down"}
    settings.CLINIC_DATA_MODE = "live"
    with pytest.raises(ImproperlyConfigured, match="synthetic data mode"):
        injected_fault("ai")


@pytest.mark.parametrize("provider", [SyntheticProvider(), RecordedFixtureProvider()])
@pytest.mark.parametrize("method", ["complete", "stream", "transcribe", "extract"])
def test_ai_faults_use_the_gateway_failure_vocabulary(
    settings: SettingsWrapper, provider: SyntheticProvider, method: str
) -> None:
    # Given: an offline adapter and a well-formed bounded request.
    request = _request(provider.allowed_host)
    settings.CLINIC_SYNTHETIC_FAULTS = {"ai": "down"}
    # When/Then: an outage and a missed deadline raise the gateway's own errors.
    with pytest.raises(ProviderUnavailableError) as unavailable:
        call_provider(provider, request=request, method=method)  # type: ignore[arg-type]
    assert type(unavailable.value) is ProviderUnavailableError
    settings.CLINIC_SYNTHETIC_FAULTS = {"ai": "slow"}
    with pytest.raises(ProviderTimeoutError):
        call_provider(provider, request=request, method=method)  # type: ignore[arg-type]
    # A malformed, over-budget reply is rejected before anything could be kept.
    settings.CLINIC_SYNTHETIC_FAULTS = {"ai": "ambiguous"}
    with pytest.raises(GatewayInputError):
        call_provider(provider, request=request, method=method)  # type: ignore[arg-type]


def test_ai_faults_never_bypass_the_egress_boundary(settings: SettingsWrapper) -> None:
    # A drill fault is evaluated only after the endpoint passed its allowlist.
    settings.CLINIC_SYNTHETIC_FAULTS = {"ai": "ambiguous"}
    request = _request("ai.elsewhere.invalid")
    with pytest.raises(ProviderUnavailableError):
        SyntheticProvider().complete(request=request)


def test_without_a_fault_the_synthetic_reply_is_unchanged(
    settings: SettingsWrapper,
) -> None:
    settings.CLINIC_SYNTHETIC_FAULTS = {"video": "down"}
    result = call_provider(SyntheticProvider(), request=_request(), method="extract")
    assert json.loads(result.content) == {"text": "Synthetic extraction"}
