"""The analyst only sees reviewed catalog parameters, never a question or facts."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING

from apps.ai.contracts import GatewayAccessDeniedError, GatewayInputError
from apps.analytics.contracts import (
    SCHEMA,
    AnalyticsAccessDeniedError,
    AnalyticsConflictError,
    AnalyticsInputError,
    Query,
    decode,
    parse_descriptor,
)
from apps.analytics.services import require_snapshot

if TYPE_CHECKING:
    from uuid import UUID

MAX_QUESTION = 500


@dataclass(frozen=True, slots=True)
class AnalystRequest:
    """The authorization version is a binding, not authority supplied by a model."""

    query: Query
    authorization_version: str

    def encode(self) -> str:
        """No user-entered text or operational data enters the gateway."""
        return json.dumps(
            {
                "schema": SCHEMA,
                "query": self.query.payload(),
                "authorization_version": self.authorization_version,
            },
            sort_keys=True,
            separators=(",", ":"),
        )


def parse_request(content: str) -> AnalystRequest:
    """Accept one bounded reference-free request envelope."""
    try:
        data = decode(content)
    except AnalyticsInputError as error:
        raise GatewayInputError from error
    if (
        not isinstance(data, dict)
        or set(data) != {"schema", "query", "authorization_version"}
        or data["schema"] != SCHEMA
        or not isinstance(data["authorization_version"], str)
        or re.fullmatch("[0-9a-f]{64}", data["authorization_version"]) is None
    ):
        raise GatewayInputError
    try:
        return AnalystRequest(
            parse_descriptor(json.dumps(data["query"])), data["authorization_version"]
        )
    except AnalyticsInputError as error:
        raise GatewayInputError from error


def validate_reply(content: str, request: AnalystRequest) -> Query:
    """No SQL, broadened window, hidden filter, forecast or invented metric is valid."""
    try:
        query = parse_descriptor(content)
    except AnalyticsInputError as error:
        raise GatewayInputError from error
    if query.payload() != request.query.payload():
        raise GatewayInputError
    return query


def synthetic_reply(content: str) -> str:
    """Deterministic offline adapter output has exactly the descriptor schema."""
    return parse_request(content).query.encode()


def recheck_request(*, clinic_id: UUID, content: str) -> None:
    """Admission, dispatch and display all revisit the live authority contract."""
    request = parse_request(content)
    try:
        require_snapshot(
            clinic_id=clinic_id,
            query=request.query,
            authorization_version=request.authorization_version,
        )
    except (AnalyticsAccessDeniedError, AnalyticsConflictError) as error:
        raise GatewayAccessDeniedError from error


def question_metric(question: str) -> str | None:
    """Clarify unrecognized or ambiguous text without sending it to inference.

    This deliberately small synthetic vocabulary is not a general language model.
    The selected date form is authoritative; relative dates need clarification.
    """
    if not isinstance(question, str) or not 1 <= len(question) <= MAX_QUESTION:
        raise AnalyticsInputError
    text = "".join(
        char
        for char in unicodedata.normalize("NFKD", question.lower())
        if not unicodedata.combining(char)
    )
    if re.search(r"\b(all|todas|todos|outra|outras|other|cross|benchmark)\b", text):
        raise AnalyticsAccessDeniedError
    aliases = {
        "utilization": ("utilization", "ocupacao", "utilizacao"),
        "no_show": ("no show", "no-show", "faltas", "absenteismo"),
        "waiting_time": ("waiting time", "tempo de espera"),
        "task_turnaround": ("task turnaround", "tempo de tarefas"),
        "receivables": ("receivables", "contas a receber"),
        "verified_collections": ("verified collections", "recebimentos verificados"),
        "net_collections": ("net collections", "recebimentos liquidos"),
        "denials": ("denials", "glosas"),
        "payout_liabilities": ("payout liabilities", "repasses a pagar"),
        "result_ack_latency": ("result acknowledgment latency", "tempo de ciencia"),
        "follow_up_overdue": ("overdue follow-ups", "seguimentos atrasados"),
        "support_load": ("support load", "carga de suporte"),
        "ai_correction_time": ("ai correction time", "tempo de correcao ia"),
        "activation": ("staff activation", "ativacao"),
        "retained_usage": ("retained usage", "uso retido"),
    }
    matches = []
    for key, words in aliases.items():
        for word in words:
            if word in text:
                remainder = text.replace(word, "").strip(" ?.!")
                tokens = set(remainder.split())
                if tokens <= {
                    "show",
                    "what",
                    "is",
                    "the",
                    "mostrar",
                    "qual",
                    "a",
                    "o",
                    "taxa",
                    "de",
                }:
                    matches.append(key)
    return matches[0] if len(set(matches)) == 1 else None
