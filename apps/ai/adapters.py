"""Provider-neutral inference with a transaction and egress boundary."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol
from urllib.parse import urlsplit

from django.db import connections

from apps.ai.brief_contracts import SCHEMA, synthetic_reply
from apps.ai.contracts import (
    MAX_TIMEOUT_SECONDS,
    GatewayInputError,
    Method,
    ProviderRequest,
    ProviderResult,
    ProviderUnavailableError,
    Usage,
    request_schema,
)
from apps.ai.decision_contracts import SCHEMA as DECISION_SCHEMA
from apps.ai.decision_contracts import synthetic_decision_reply

if TYPE_CHECKING:
    from collections.abc import Iterator


class ProviderProtocol(Protocol):
    """All inference capabilities go through the same reviewed adapter surface."""

    key: str

    @property
    def real_enabled(self) -> bool:
        """Remain read-only so callers cannot enable a real implementation."""
        ...

    def complete(self, *, request: ProviderRequest) -> ProviderResult:
        """Return a bounded completion."""
        ...

    def stream(self, *, request: ProviderRequest) -> Iterator[ProviderResult]:
        """Yield bounded deltas; the last delta carries cumulative usage."""
        ...

    def transcribe(self, *, request: ProviderRequest) -> ProviderResult:
        """Return a synthetic transcript from an encrypted input reference."""
        ...

    def extract(self, *, request: ProviderRequest) -> ProviderResult:
        """Return a closed-schema extraction candidate, never a domain write."""
        ...


class AdapterBase:
    """Every concrete operation checks transactions and its exact endpoint."""

    real_enabled: Final = False
    key = ""
    allowed_host = ""

    def check_boundary(self, request: ProviderRequest) -> None:
        """Deny transaction-bound calls and every unapproved egress destination."""
        if any(
            db.in_atomic_block or (db.connection is not None and not db.autocommit)
            for db in connections.all(initialized_only=True)
        ):
            raise ProviderUnavailableError
        try:
            endpoint = urlsplit(request.endpoint)
        except ValueError:
            raise ProviderUnavailableError from None
        if (
            endpoint.scheme != "https"
            or endpoint.netloc != self.allowed_host
            or endpoint.path != "/v1/inference"
            or endpoint.query
            or endpoint.fragment
            or not 1 <= request.timeout_seconds <= MAX_TIMEOUT_SECONDS
        ):
            raise ProviderUnavailableError


class SyntheticProvider(AdapterBase):
    """Deterministic offline provider; it never imports a network client."""

    key = "synthetic"
    allowed_host = "ai.synthetic.invalid"

    def complete(self, *, request: ProviderRequest) -> ProviderResult:
        """Return a fixed review-only draft without echoing source content."""
        self.check_boundary(request)
        content = "Synthetic draft. Human review required."
        schema = request_schema(request.content)
        if schema == DECISION_SCHEMA:
            content = synthetic_decision_reply(request.content)
        elif schema == SCHEMA:
            content = synthetic_reply(request.content)
        return ProviderResult(
            content,
            Usage(
                tokens_in=min(request.limits.tokens_in, 4),
                tokens_out=min(request.limits.tokens_out, 6),
            ),
        )

    def stream(self, *, request: ProviderRequest) -> Iterator[ProviderResult]:
        """Use the same exact boundary for each requested streaming operation."""
        yield self.complete(request=request)

    def transcribe(self, *, request: ProviderRequest) -> ProviderResult:
        """Return a fixed transcript with exact, bounded whole audio seconds."""
        self.check_boundary(request)
        return ProviderResult(
            "Synthetic transcript. Human review required.",
            Usage(audio_seconds=min(request.limits.audio_seconds, 1)),
        )

    def extract(self, *, request: ProviderRequest) -> ProviderResult:
        """Produce only the extraction schema the gateway accepts."""
        result = self.complete(request=request)
        return ProviderResult('{"text":"Synthetic extraction"}', result.usage)


class RecordedFixtureProvider(SyntheticProvider):
    """A versioned, offline recording, not a user-selected file or URL."""

    key = "recorded_fixture"
    allowed_host = "recorded.synthetic.invalid"

    def complete(self, *, request: ProviderRequest) -> ProviderResult:
        """Load the one reviewed recording by its closed model identifier."""
        self.check_boundary(request)
        return self._record(request, "complete")

    def stream(self, *, request: ProviderRequest) -> Iterator[ProviderResult]:
        """Replay the recording's terminal delta with cumulative metering."""
        self.check_boundary(request)
        yield self._record(request, "stream")

    def transcribe(self, *, request: ProviderRequest) -> ProviderResult:
        """Replay the recorded transcript and its exact whole-second usage."""
        self.check_boundary(request)
        return self._record(request, "transcribe")

    def extract(self, *, request: ProviderRequest) -> ProviderResult:
        """Replay a closed-schema extraction, not a generated completion."""
        self.check_boundary(request)
        return self._record(request, "extract")

    @staticmethod
    def _record(request: ProviderRequest, method: Method) -> ProviderResult:
        if request.model_id != "fixture-v1":
            raise ProviderUnavailableError
        payload = json.loads(
            (Path(__file__).parent / "fixtures" / "v1.json").read_text(encoding="utf-8")
        )
        if (
            not isinstance(payload, dict)
            or set(payload) != {"schema_version", "data_class", "model_id", "responses"}
            or payload["schema_version"] != 1
            or payload["data_class"] != "synthetic"
            or payload["model_id"] != "fixture-v1"
            or not isinstance(payload["responses"], dict)
            or set(payload["responses"])
            != {"complete", "stream", "transcribe", "extract"}
        ):
            raise ProviderUnavailableError
        record = payload["responses"][method]
        if (
            not isinstance(record, dict)
            or set(record) != {"content", "usage"}
            or not isinstance(record["content"], str)
            or not isinstance(record["usage"], dict)
            or set(record["usage"])
            != {"tokens_in", "tokens_out", "tokens_cached", "audio_seconds"}
        ):
            raise ProviderUnavailableError
        usage = record["usage"]
        return ProviderResult(
            record["content"],
            Usage(
                tokens_in=usage["tokens_in"],
                tokens_out=usage["tokens_out"],
                tokens_cached=usage["tokens_cached"],
                audio_seconds=usage["audio_seconds"],
            ),
        )


_PROVIDERS: Final[dict[str, ProviderProtocol]] = {}


def register_provider(provider: ProviderProtocol) -> None:
    """Register only explicitly synthetic implementations during app startup."""
    if provider.real_enabled or provider.key not in {"synthetic", "recorded_fixture"}:
        raise GatewayInputError
    _PROVIDERS[provider.key] = provider


def provider_for(key: str) -> ProviderProtocol:
    """Refuse unknown registrations instead of choosing a vendor implicitly."""
    provider = _PROVIDERS.get(key)
    if provider is None or provider.real_enabled:
        raise ProviderUnavailableError
    return provider
