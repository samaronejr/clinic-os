"""RFC 8785 action v1 parsing; reject ambiguity before any policy decision."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final
from uuid import UUID

import rfc8785

from apps.ai.action_contracts import (
    AUTONOMY,
    MAX_ACTION_BYTES,
    OPERATIONS,
    ActionInputError,
)

type JSON = str | int | bool | None | list[JSON] | dict[str, JSON]
_UUID: Final = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
_HASH: Final = re.compile(r"[0-9a-f]{64}")
_INSTANT: Final = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?Z")
MAX_COLLECTION: Final = 100
FIELDS: Final = frozenset(
    {
        "schema_version",
        "action_id",
        "operation",
        "organization_id",
        "clinic_id",
        "patient_id",
        "subject",
        "source_versions",
        "document",
        "recipients",
        "amounts",
        "provider",
        "parameters",
        "not_before",
        "expires_at",
        "approver_authority_snapshots",
        "approval_policy",
        "compensation_policy",
        "idempotency_key",
    }
)


def _pairs(pairs: list[tuple[str, JSON]]) -> dict[str, JSON]:
    result: dict[str, JSON] = {}
    for key, value in pairs:
        if key in result:
            raise ActionInputError
        result[key] = value
    return result


def _number(_value: str) -> None:
    raise ActionInputError


def closed(value: JSON, keys: set[str] | frozenset[str]) -> dict[str, JSON]:
    """Require all and only the named keys; no optional unknown fields."""
    if not isinstance(value, dict) or set(value) != keys:
        raise ActionInputError
    return value


def text(value: JSON, *, maximum: int = 256) -> str:
    """Reject unbounded text and invalid Unicode, without normalizing it."""
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        raise ActionInputError
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ActionInputError from None
    return value


def identifier(value: JSON) -> UUID:
    """Use one canonical spelling for every opaque selector."""
    if not isinstance(value, str) or _UUID.fullmatch(value) is None:
        raise ActionInputError
    return UUID(value)


def integer(value: JSON, *, maximum: int = 2**31 - 1, minimum: int = 1) -> int:
    """Booleans, floating-point and out-of-domain integers are not counts."""
    if type(value) is not int or not minimum <= value <= maximum:
        raise ActionInputError
    return value


def instant(value: JSON) -> datetime:
    """Require an absolute UTC instant, not a locale-dependent date."""
    if not isinstance(value, str) or _INSTANT.fullmatch(value) is None:
        raise ActionInputError
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise ActionInputError from None


def digest_text(value: JSON) -> str:
    """Parse a SHA-256 hex digest."""
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise ActionInputError
    return value


def ordered(value: JSON) -> list[JSON]:
    """Require unique set entries in ascending RFC 8785 encoded-byte order."""
    if not isinstance(value, list) or len(value) > MAX_COLLECTION:
        raise ActionInputError
    encodings = [rfc8785.dumps(item) for item in value]
    if encodings != sorted(set(encodings)):
        raise ActionInputError
    return value


def _references(value: JSON) -> None:
    for item in ordered(value):
        identifier(item)


def _sources(value: JSON) -> None:
    for item in ordered(value):
        row = closed(item, {"kind", "id", "version", "revision", "content_sha256"})
        if row["kind"] not in ("workflow_definition", "ai_artifact"):
            raise ActionInputError
        identifier(row["id"])
        integer(row["version"])
        integer(row["revision"])
        digest_text(row["content_sha256"])


def _recipients(value: JSON) -> None:
    for item in ordered(value):
        row = closed(
            item,
            {
                "kind",
                "id",
                "destination",
                "version",
                "consent_refs",
                "delegation_refs",
            },
        )
        if row["kind"] not in ("synthetic", "email", "sms", "whatsapp", "portal"):
            raise ActionInputError
        identifier(row["id"])
        text(row["destination"])
        integer(row["version"])
        _references(row["consent_refs"])
        _references(row["delegation_refs"])


def _authority(value: JSON) -> None:
    """Keep membership order identical to SQL agent_bound_snapshot.

    In v1, encoded membership objects compare bundle_version (1/2), id, then
    role. Reject reordered input rather than silently changing approved bytes.
    """
    for item in ordered(value):
        row = closed(item, {"user_id", "permission", "memberships"})
        identifier(row["user_id"])
        text(row["permission"], maximum=64)
        for member in ordered(row["memberships"]):
            grant = closed(member, {"id", "role", "bundle_version"})
            identifier(grant["id"])
            text(grant["role"], maximum=24)
            integer(grant["bundle_version"], maximum=2)


def _amounts(value: JSON) -> None:
    row = closed(value, {"currency", "centavos", "direction", "allocations"})
    if (
        row["currency"] != "BRL"
        or not isinstance(row["centavos"], str)
        or re.fullmatch(r"0|[1-9][0-9]{0,17}", row["centavos"]) is None
        or row["direction"] not in ("none", "debit", "credit")
    ):
        raise ActionInputError
    for item in ordered(row["allocations"]):
        allocation = closed(item, {"id", "centavos"})
        identifier(allocation["id"])
        if (
            not isinstance(allocation["centavos"], str)
            or re.fullmatch(r"0|[1-9][0-9]{0,17}", allocation["centavos"]) is None
        ):
            raise ActionInputError


@dataclass(frozen=True, slots=True)
class ActionPayload:
    """Immutable canonical bytes are the only persisted approval payload."""

    canonical: bytes = field(repr=False)

    @property
    def digest(self) -> str:
        """Hash exact canonical bytes, with no Unicode normalization."""
        return hashlib.sha256(self.canonical).hexdigest()

    def data(self) -> dict[str, JSON]:
        """Return a fresh decoded copy; mutation cannot alter the approved value."""
        value: JSON = json.loads(self.canonical)
        return closed(value, FIELDS)


def parse_payload(raw: str) -> ActionPayload:
    """Parse the closed action v1 envelope and its registered argument schema."""
    if not isinstance(raw, str) or len(raw) > MAX_ACTION_BYTES:
        raise ActionInputError
    try:
        value: JSON = json.loads(
            raw,
            object_pairs_hook=_pairs,
            parse_float=_number,
            parse_constant=_number,
        )
        row = closed(value, FIELDS)
        _scope_terms(row)
        _effect_terms(row)
        _approval_terms(row)
        canonical = rfc8785.dumps(row)
    except (ValueError, TypeError, UnicodeError, rfc8785.CanonicalizationError):
        raise ActionInputError from None
    if len(canonical) > MAX_ACTION_BYTES:
        raise ActionInputError
    return ActionPayload(canonical)


def _scope_terms(row: dict[str, JSON]) -> None:
    integer(row["schema_version"], maximum=1)
    for key in ("action_id", "organization_id", "clinic_id", "idempotency_key"):
        identifier(row[key])
    if row["patient_id"] is not None:
        identifier(row["patient_id"])
    subject = closed(row["subject"], {"kind", "id", "expected_state", "revision"})
    if subject["kind"] not in ("workflow_definition", "ai_artifact"):
        raise ActionInputError
    identifier(subject["id"])
    text(subject["expected_state"], maximum=24)
    integer(subject["revision"])
    _sources(row["source_versions"])
    if row["document"] is not None:
        document = closed(row["document"], {"version", "sha256"})
        integer(document["version"])
        digest_text(document["sha256"])
    if instant(row["not_before"]) >= instant(row["expires_at"]):
        raise ActionInputError


def _effect_terms(row: dict[str, JSON]) -> None:
    operation = closed(row["operation"], {"name", "version"})
    if operation["name"] not in OPERATIONS:
        raise ActionInputError
    integer(operation["version"], maximum=1)
    _recipients(row["recipients"])
    _amounts(row["amounts"])
    provider = closed(
        row["provider"],
        {
            "capability_version",
            "account",
            "environment",
            "api_version",
        },
    )
    identifier(provider["capability_version"])
    for key in ("account", "environment", "api_version"):
        text(provider[key], maximum=128)
    if operation["name"] == "comms.synthetic_reminder":
        arguments = closed(row["parameters"], {"template"})
        if arguments["template"] != "logistics-v1":
            raise ActionInputError
    else:
        arguments = closed(row["parameters"], {"due_at"})
        instant(arguments["due_at"])


def _approval_terms(row: dict[str, JSON]) -> None:
    _authority(row["approver_authority_snapshots"])
    policy = closed(
        row["approval_policy"], {"policy_id", "version", "quorum", "autonomy"}
    )
    identifier(policy["policy_id"])
    integer(policy["version"])
    integer(policy["quorum"], maximum=2, minimum=0)
    if policy["autonomy"] not in AUTONOMY:
        raise ActionInputError
    compensation = closed(row["compensation_policy"], {"mode", "version"})
    if compensation["mode"] not in ("manual", "synthetic_cancel", "none"):
        raise ActionInputError
    integer(compensation["version"], maximum=1)
