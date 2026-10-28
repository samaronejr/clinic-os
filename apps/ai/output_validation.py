"""Bound provider results before encryption; output remains untrusted data."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, assert_never

from apps.ai.brief_contracts import SCHEMA, parse_request, validate_reply
from apps.ai.contracts import (
    MAX_CONTENT_BYTES,
    GatewayInputError,
    Method,
    ProviderRequest,
    ProviderResult,
    Usage,
    request_schema,
)
from apps.ai.decision_contracts import SCHEMA as DECISION_SCHEMA
from apps.ai.decision_contracts import parse_decision_request, validate_decision_reply

if TYPE_CHECKING:
    from apps.ai.adapters import ProviderProtocol


def parse_method(raw: str) -> Method:
    """Parse the persisted closed method vocabulary."""
    match raw:
        case "complete":
            return "complete"
        case "stream":
            return "stream"
        case "transcribe":
            return "transcribe"
        case "extract":
            return "extract"
        case _:
            raise GatewayInputError


def validate_result(
    result: ProviderResult, *, limits: Usage, method: Method
) -> ProviderResult:
    """Reject excess usage and nonconforming extraction without echoing it."""
    if (
        type(result) is not ProviderResult
        or type(result.content) is not str
        or not result.content
        or len(result.content.encode("utf-8")) > MAX_CONTENT_BYTES
        or type(result.usage) is not Usage
    ):
        raise GatewayInputError
    usage = result.usage
    if (
        usage.tokens_in > limits.tokens_in
        or usage.tokens_out > limits.tokens_out
        or usage.audio_seconds > limits.audio_seconds
    ):
        raise GatewayInputError
    if method == "extract":
        try:
            pairs = json.loads(result.content, object_pairs_hook=list)
        except (json.JSONDecodeError, RecursionError) as error:
            raise GatewayInputError from error
        if (
            not isinstance(pairs, list)
            or len(pairs) != 1
            or not isinstance(pairs[0], tuple)
            or pairs[0][0] != "text"
            or type(pairs[0][1]) is not str
        ):
            raise GatewayInputError
    return result


def call_provider(
    provider: ProviderProtocol, *, request: ProviderRequest, method: Method
) -> ProviderResult:
    """Dispatch every capability through the registered neutral protocol."""
    match method:
        case "complete":
            result = provider.complete(request=request)
        case "transcribe":
            result = provider.transcribe(request=request)
        case "extract":
            result = provider.extract(request=request)
        case "stream":
            chunks: list[str] = []
            size = 0
            usage = Usage()
            for delta in provider.stream(request=request):
                validate_result(delta, limits=request.limits, method=method)
                size += len(delta.content.encode("utf-8"))
                if size > MAX_CONTENT_BYTES:
                    raise GatewayInputError
                chunks.append(delta.content)
                usage = delta.usage
            result = ProviderResult("".join(chunks), usage)
        case unreachable:
            assert_never(unreachable)
    result = validate_result(result, limits=request.limits, method=method)
    schema = request_schema(request.content)
    if schema == DECISION_SCHEMA:
        validate_decision_reply(result.content, parse_decision_request(request.content))
    elif schema == SCHEMA:
        validate_reply(result.content, parse_request(request.content))
    return result
