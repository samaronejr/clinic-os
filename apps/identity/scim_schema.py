"""Bounded SCIM Users/Groups input; equality search uses scoped blind indexes."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.utils.crypto import salted_hmac

from apps.identity.scim_paths import OPAQUE_ID_PATTERN

if TYPE_CHECKING:
    from collections.abc import Mapping
    from uuid import UUID

MAX_MEMBERS = 1000
MAX_RESOURCE_BYTES = 32768
MAX_EMAILS = 5
MAX_FILTER_LENGTH = 1024
MAX_PATCH_OPERATIONS = 20
FIRST_PRINTABLE_CODEPOINT = 32
SCHEMAS = {
    "Users": "urn:ietf:params:scim:schemas:core:2.0:User",
    "Groups": "urn:ietf:params:scim:schemas:core:2.0:Group",
}


class ScimInputError(ValueError):
    """Return fixed protocol error categories, never user-controlled text."""

    scim_type = "invalidValue"


class ScimConflictError(ScimInputError):
    """A stale version, conflicting external identity or duplicate username."""

    scim_type = "uniqueness"


def _text(value: object, maximum: int = 255) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value.strip()) <= maximum
        and not any(ord(character) < FIRST_PRINTABLE_CODEPOINT for character in value)
    )


def _user_attributes(result: dict[str, object]) -> None:
    """Validate the bounded user subset without accepting authority attributes."""
    if type(result.setdefault("active", True)) is not bool:
        raise ScimInputError
    name = result.get("name", {})
    if (
        not isinstance(name, dict)
        or set(name) - {"formatted", "familyName", "givenName"}
        or not all(_text(item) for item in name.values())
    ):
        raise ScimInputError
    emails = result.get("emails", [])
    if not isinstance(emails, list) or len(emails) > MAX_EMAILS:
        raise ScimInputError
    for email in emails:
        if (
            not isinstance(email, dict)
            or set(email) - {"value", "type", "primary"}
            or not _text(email.get("value"))
            or ("type" in email and email["type"] not in {"work", "home", "other"})
            or ("primary" in email and type(email["primary"]) is not bool)
        ):
            raise ScimInputError
        try:
            validate_email(email["value"])
        except ValidationError as error:
            raise ScimInputError from error


def _group_attributes(result: dict[str, object]) -> None:
    """Keep group membership selectors inside this connection's opaque namespace."""
    members = result.setdefault("members", [])
    if not isinstance(members, list) or len(members) > MAX_MEMBERS:
        raise ScimInputError
    identifiers = set()
    for member in members:
        if (
            not isinstance(member, dict)
            or set(member) != {"value"}
            or not isinstance(member["value"], str)
            or re.fullmatch(OPAQUE_ID_PATTERN, member["value"]) is None
        ):
            raise ScimInputError
        identifiers.add(member["value"])
    result["members"] = [{"value": identifier} for identifier in sorted(identifiers)]


def attributes(*, kind: str, value: Mapping[str, object]) -> dict[str, object]:
    """Parse the advertised core subset; unknown attributes cannot affect authority."""
    allowed = (
        {"schemas", "userName", "externalId", "active", "name", "emails"}
        if kind == "Users"
        else {"schemas", "displayName", "externalId", "members"}
    )
    if kind not in SCHEMAS or set(value) - allowed:
        raise ScimInputError
    result = dict(value)
    schemas = result.pop("schemas", None)
    if schemas is not None and schemas != [SCHEMAS[kind]]:
        raise ScimInputError
    label = "userName" if kind == "Users" else "displayName"
    if not _text(result.get(label), 150):
        raise ScimInputError
    if "externalId" in result and not _text(result["externalId"]):
        raise ScimInputError
    if kind == "Users":
        _user_attributes(result)
    else:
        _group_attributes(result)
    return result


def blind_index(*, connection_id: UUID, field: str, value: str) -> bytes:
    """Separate subject and case-insensitive directory-name namespaces."""
    normalized = value if field == "externalId" else value.casefold()
    return salted_hmac(
        f"identity.scim.{connection_id}.{field}", normalized, algorithm="sha256"
    ).digest()


def equality_filter(value: str) -> tuple[str, str]:
    """Only body-supplied exact equality is advertised; no plaintext SQL search."""
    matched = re.fullmatch(
        r'(userName|displayName|externalId) eq ("(?:[^"\\]|\\.)*")', value
    )
    if matched is None or len(value) > MAX_FILTER_LENGTH:
        raise ScimInputError
    try:
        term = json.loads(matched.group(2))
    except json.JSONDecodeError as error:
        raise ScimInputError from error
    if not _text(term):
        raise ScimInputError
    return matched.group(1), term


def _patch_members(*, verb: str, value: object, previous: object) -> object:
    """Apply only advertised whole-member-list operations."""
    if verb == "replace":
        return value
    if verb == "remove":
        return []
    if verb == "add" and isinstance(value, list) and isinstance(previous, list):
        return previous + value
    raise ScimInputError


def patched(
    *, kind: str, current: Mapping[str, object], operations: object
) -> dict[str, object]:
    """Implement bounded replace/add/remove without executing SCIM path expressions."""
    if (
        not isinstance(operations, list)
        or not 1 <= len(operations) <= MAX_PATCH_OPERATIONS
    ):
        raise ScimInputError
    result = dict(current)
    for operation in operations:
        if not isinstance(operation, dict) or set(operation) - {"op", "path", "value"}:
            raise ScimInputError
        verb, path, value = (
            operation.get("op"),
            operation.get("path"),
            operation.get("value"),
        )
        if not isinstance(verb, str):
            raise ScimInputError
        verb = verb.casefold()
        if path is None and verb in {"add", "replace"} and isinstance(value, dict):
            result.update(value)
        elif path == "members" and kind == "Groups":
            result["members"] = _patch_members(
                verb=verb, value=value, previous=result.get("members", [])
            )
        elif isinstance(path, str) and path in {
            "active",
            "userName",
            "name",
            "emails",
            "displayName",
        }:
            if verb in {"add", "replace"}:
                result[path] = value
            elif verb == "remove" and path in {"name", "emails"}:
                result.pop(path, None)
            else:
                raise ScimInputError
        else:
            raise ScimInputError
    return attributes(kind=kind, value=result)
