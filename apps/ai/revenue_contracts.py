"""Closed AI-06 facts and outputs: no narrative, instruction or monetary tool."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Final, TypedDict

from apps.ai.action_contracts import ActionInputError
from apps.ai.action_payload import JSON, closed, integer
from apps.ai.contracts import MAX_CONTENT_BYTES, GatewayInputError

SCHEMA: Final = "revenue-cycle-v1"
MAX_ROWS: Final = 64
MAX_MONEY: Final = 2**53 - 1
KINDS: Final = frozenset({"appeal.draft", "discrepancy.explain"})


class Reply(TypedDict):
    """A complete list of source-backed findings; omissions also fail closed."""

    schema: str
    items: list[dict[str, JSON]]


def _pairs(pairs: list[tuple[str, JSON]]) -> dict[str, JSON]:
    result: dict[str, JSON] = {}
    for key, value in pairs:
        if key in result:
            raise GatewayInputError
        result[key] = value
    return result


def _number(_value: str) -> None:
    raise GatewayInputError


def decode(raw: str) -> JSON:
    """Reject duplicate keys, fractional numbers, constants and excessive bodies."""
    if not isinstance(raw, str) or len(raw.encode()) > MAX_CONTENT_BYTES:
        raise GatewayInputError
    try:
        value: JSON = json.loads(
            raw, object_pairs_hook=_pairs, parse_float=_number, parse_constant=_number
        )
    except (ValueError, RecursionError, UnicodeError):
        raise GatewayInputError from None
    return value


def _code(value: JSON, length: int) -> str:
    if not isinstance(value, str) or re.fullmatch(rf"[0-9]{{{length}}}", value) is None:
        raise GatewayInputError
    return value


@dataclass(frozen=True, slots=True)
class RevenueFacts:
    """Only minimum claim codes and financial facts; no patient identifiers."""

    guide: dict[str, JSON]
    journal: dict[str, JSON]
    returns: tuple[dict[str, JSON], ...]
    payments: dict[str, JSON]

    def encode(self) -> str:
        """Send aliases, never source ids, hashes or payer prose."""
        return json.dumps(
            {
                "schema": SCHEMA,
                "guide": self.guide,
                "journal": self.journal,
                "returns": list(self.returns),
                "payments": self.payments,
            },
            separators=(",", ":"),
            sort_keys=True,
        )


def _procedures(guide: dict[str, JSON]) -> list[dict[str, JSON]]:
    procedures = guide["procedures"]
    if not isinstance(procedures, list) or not 1 <= len(procedures) <= MAX_ROWS:
        raise GatewayInputError
    parsed = []
    total = 0
    for raw_procedure in procedures:
        procedure = closed(raw_procedure, {"table", "code", "quantity", "unit_minor"})
        _code(procedure["table"], 2)
        if (
            not isinstance(procedure["code"], str)
            or re.fullmatch(r"[0-9]{1,10}", procedure["code"]) is None
        ):
            raise GatewayInputError
        quantity = integer(procedure["quantity"], maximum=9999)
        unit = integer(procedure["unit_minor"], maximum=MAX_MONEY)
        total += quantity * unit
        parsed.append(procedure)
    if total != guide["total_minor"]:
        raise GatewayInputError
    return parsed


def _returns(
    value: JSON, procedures: list[dict[str, JSON]]
) -> tuple[dict[str, JSON], ...]:
    if not isinstance(value, list) or len(value) > MAX_ROWS:
        raise GatewayInputError
    parsed: list[dict[str, JSON]] = []
    sequences: set[int] = set()
    for position, raw_return in enumerate(value, 1):
        row = closed(
            raw_return,
            {
                "source",
                "sequence",
                "charged_minor",
                "accepted_minor",
                "denied_minor",
                "code",
            },
        )
        sequence = integer(row["sequence"], maximum=len(procedures))
        if row["source"] != f"R{position}" or sequence in sequences:
            raise GatewayInputError
        sequences.add(sequence)
        charged, accepted, denied = (
            integer(row[key], maximum=MAX_MONEY, minimum=0)
            for key in ("charged_minor", "accepted_minor", "denied_minor")
        )
        procedure = procedures[sequence - 1]
        if (
            charged != accepted + denied
            or charged
            != integer(procedure["quantity"], maximum=9999)
            * integer(procedure["unit_minor"], maximum=MAX_MONEY)
            or (denied > 0 and not _code(row["code"], 4))
            or (denied == 0 and row["code"] != "")
        ):
            raise GatewayInputError
        parsed.append(row)
    return tuple(parsed)


def parse_model_input(raw: str) -> RevenueFacts:
    """Validate the minimized input independently of the retrieval implementation."""
    try:
        root = closed(
            decode(raw), {"schema", "guide", "journal", "returns", "payments"}
        )
        if root["schema"] != SCHEMA:
            raise GatewayInputError
        guide = closed(root["guide"], {"source", "total_minor", "procedures"})
        journal = closed(root["journal"], {"source", "net_minor", "recognized"})
        payments = closed(root["payments"], {"source", "allocated_minor"})
        if (guide["source"], journal["source"], payments["source"]) != (
            "G1",
            "J1",
            "P1",
        ):
            raise GatewayInputError
        integer(guide["total_minor"], maximum=MAX_MONEY)
        integer(payments["allocated_minor"], maximum=MAX_MONEY, minimum=0)
        if type(journal["recognized"]) is not bool:
            raise GatewayInputError
        if journal["net_minor"] is not None:
            integer(journal["net_minor"], maximum=MAX_MONEY, minimum=0)
        elif journal["recognized"]:
            raise GatewayInputError
        parsed = _returns(root["returns"], _procedures(guide))
    except ActionInputError:
        raise GatewayInputError from None
    return RevenueFacts(guide, journal, parsed, payments)


def expected_reply(facts: RevenueFacts) -> Reply:
    """Rules determine every discrepancy; the model cannot supply missing facts."""
    items: list[dict[str, JSON]] = []

    def finding(code: str, citations: list[JSON]) -> None:
        items.append(
            {
                "kind": "discrepancy.explain",
                "finding": code,
                "citations": citations,
                "procedure": None,
            }
        )

    if facts.journal["recognized"] is not True:
        finding("recognition_evidence_missing", ["G1", "J1"])
    elif facts.journal["net_minor"] != facts.guide["total_minor"]:
        finding("claim_journal_mismatch", ["G1", "J1"])
    if not facts.returns:
        finding("return_evidence_missing", ["G1"])
    accepted = 0
    for row in facts.returns:
        accepted += integer(row["accepted_minor"], maximum=MAX_MONEY, minimum=0)
        if integer(row["denied_minor"], maximum=MAX_MONEY, minimum=0):
            citations: list[JSON] = ["G1", row["source"]]
            finding("claim_denied", citations)
            procedures = facts.guide["procedures"]
            if not isinstance(procedures, list):
                raise GatewayInputError
            items.append(
                {
                    "kind": "appeal.draft",
                    "finding": "claim_denied",
                    "citations": citations,
                    "procedure": procedures[integer(row["sequence"]) - 1],
                }
            )
    allocated = integer(facts.payments["allocated_minor"], maximum=MAX_MONEY, minimum=0)
    if accepted > allocated:
        finding(
            "payment_evidence_missing",
            ["P1", *[row["source"] for row in facts.returns]],
        )
    elif allocated > accepted and facts.returns:
        finding(
            "payment_claim_mismatch",
            ["P1", *[row["source"] for row in facts.returns]],
        )
    return {"schema": SCHEMA, "items": items}


def validate_reply(raw: str, facts: RevenueFacts) -> tuple[dict[str, JSON], ...]:
    """Compare every assertion and billed procedure to encounter-bound facts."""
    if json.dumps(decode(raw), sort_keys=True) != json.dumps(
        expected_reply(facts), sort_keys=True
    ):
        raise GatewayInputError
    return tuple(expected_reply(facts)["items"])


def synthetic_reply(raw: str) -> str:
    """Offline deterministic classification, not a claim of model understanding."""
    return json.dumps(expected_reply(parse_model_input(raw)), separators=(",", ":"))
