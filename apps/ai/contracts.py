"""Closed gateway inputs, exact prices, and content-free refusal results."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Final, Literal

from django.utils.translation import gettext_noop

if TYPE_CHECKING:
    from uuid import UUID


class Purpose(StrEnum):
    """The versioned purpose vocabulary, shared with telemetry."""

    SCRIBE_ASR = "scribe_asr"
    SCRIBE_DRAFT = "scribe_draft"
    BRIEF = "brief"
    EXTRACTION = "extraction"
    FRONT_DESK = "front_desk"
    ANALYST = "analyst"
    DECISION_SUPPORT = "decision_support"


type Method = Literal["complete", "stream", "transcribe", "extract"]
type Reason = Literal[
    "disabled",
    "budget_exhausted",
    "configuration_unavailable",
    "circuit_open",
    "provider_timeout",
    "provider_unavailable",
    "invalid_output",
    "authority_revoked",
]
PURPOSES: Final = tuple(p.value for p in Purpose)
MAX_UNITS: Final = 1_000_000
MAX_CONTENT_BYTES: Final = 1_048_576
MAX_MICROS: Final = 10**15
MAX_TIMEOUT_SECONDS: Final = 60
MAX_MODEL_TEXT: Final = 128
MICROS_PER_CENTAVO: Final = 10_000
REGION_CLASSES: Final = {
    "br-synthetic": "BR",
    "br-synthetic-secondary": "BR",
    "eu-synthetic": "EU",
    "us-synthetic": "US",
    "sa-east-1": "BR",
}


class GatewayInputError(ValueError):
    """Reject malformed input without reflecting content or selectors."""

    def __init__(self) -> None:
        """Keep error text safe for boundary handlers."""
        super().__init__("invalid AI gateway input")


class GatewayAccessDeniedError(PermissionError):
    """Use the same denial for unknown, foreign, and unauthorized records."""

    def __init__(self) -> None:
        """Expose no identity or record detail."""
        super().__init__("AI gateway access denied")


class ProviderUnavailableError(RuntimeError):
    """A synthetic adapter failed without disclosing its input."""

    def __init__(self) -> None:
        """Keep every adapter failure safe for boundary handlers."""
        super().__init__("AI provider unavailable")


class ProviderTimeoutError(ProviderUnavailableError):
    """The provider exceeded its bounded request deadline."""


def request_schema(content: str) -> str | None:
    """Select only a structural root schema; generic inputs may be plain text."""
    try:
        data: object = json.loads(content)
    except (json.JSONDecodeError, RecursionError):
        return None
    if isinstance(data, dict):
        schema = data.get("schema")
        if isinstance(schema, str):
            return schema
    return None


@dataclass(frozen=True, slots=True)
class Usage:
    """Input tokens include cached tokens; audio is counted in whole seconds."""

    tokens_in: int = 0
    tokens_out: int = 0
    tokens_cached: int = 0
    audio_seconds: int = 0

    def __post_init__(self) -> None:
        """Parse integer units once, refusing bool, floats, and negative values."""
        if (
            any(
                type(value) is not int or not 0 <= value <= MAX_UNITS
                for value in (
                    self.tokens_in,
                    self.tokens_out,
                    self.tokens_cached,
                    self.audio_seconds,
                )
            )
            or self.tokens_cached > self.tokens_in
        ):
            raise GatewayInputError


@dataclass(frozen=True, slots=True)
class Price:
    """Integer micro-BRL per token or whole audio second."""

    input_micros: int
    output_micros: int
    cached_micros: int
    audio_micros: int

    def __post_init__(self) -> None:
        """Bound arithmetic to the persisted bigint domain."""
        if any(
            type(value) is not int or not 0 <= value <= MAX_UNITS
            for value in (
                self.input_micros,
                self.output_micros,
                self.cached_micros,
                self.audio_micros,
            )
        ):
            raise GatewayInputError

    def cost(self, usage: Usage) -> int:
        """Charge cached tokens once, retaining all fractional centavos."""
        return (
            (usage.tokens_in - usage.tokens_cached) * self.input_micros
            + usage.tokens_cached * self.cached_micros
            + usage.tokens_out * self.output_micros
            + usage.audio_seconds * self.audio_micros
        )

    def reservation(self, limits: Usage) -> int:
        """Reserve the worst input/cache mix, never a floating-point estimate."""
        return (
            limits.tokens_in * max(self.input_micros, self.cached_micros)
            + limits.tokens_out * self.output_micros
            + limits.audio_seconds * self.audio_micros
        )


@dataclass(frozen=True, slots=True)
class Budget:
    """One call's unit ceilings and its caller-narrowed spending ceiling."""

    limits: Usage
    maximum_micros: int

    def __post_init__(self) -> None:
        """Bound the caller's spending ceiling without replacing org policy."""
        if (
            type(self.maximum_micros) is not int
            or not 0 <= self.maximum_micros <= MAX_MICROS
        ):
            raise GatewayInputError


@dataclass(frozen=True, slots=True)
class ProviderRequest:
    """Ephemeral plaintext, never a task argument or a logging representation."""

    model_id: str
    endpoint: str
    prompt: str = field(repr=False)
    content: str = field(repr=False)
    limits: Usage
    timeout_seconds: int


@dataclass(frozen=True, slots=True)
class ProviderResult:
    """Untrusted provider data; the gateway checks shape and unit ceilings."""

    content: str = field(repr=False)
    usage: Usage


@dataclass(frozen=True, slots=True)
class InvocationResult:
    """A content-free command result with a complete manual alternative."""

    invocation_id: UUID | None
    operation_id: UUID | None
    reason: Reason | None = None
    # Telemetry imports this vocabulary while Django settings are still loading.
    message_key: str = field(
        default_factory=lambda: gettext_noop(
            "AI request queued. You can continue working manually."
        )
    )


class GatewayRefusedError(RuntimeError):
    """An internal closed-set decision; content never becomes an error message."""

    def __init__(self, reason: Reason) -> None:
        """Carry only the reason code to the result boundary."""
        self.reason = reason
        super().__init__(reason)
