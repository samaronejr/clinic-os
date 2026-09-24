# ruff: noqa: ANN401 - Django field hooks receive and return values as Any
"""Envelope-encrypted model fields over the protected_* SQL boundary.

Each field stores a versioned tenant envelope in a ``bytea`` column and
encrypts/decrypts through ``clinic_app.protected_encrypt`` /
``protected_decrypt`` inside the current transaction. The tenant is resolved
in-database from ``app.current_tenant`` (staff/owner context) or the live
``app.current_patient_session`` row (patient-session context); a missing or
mismatched context, missing key or wrong KEK raises ``EnvelopeError`` and
nothing is stored or returned. There is no plaintext column and no fallback.

Encrypted columns cannot participate in SQL predicates, ordering or
constraints on plaintext semantics; approved query surfaces (patient
registry search) run inside reviewed SECURITY DEFINER functions.
"""

from __future__ import annotations

import json
from datetime import date
from typing import TYPE_CHECKING, Any

import rfc8785
from django.core.exceptions import ValidationError
from django.db import models

from apps.core.idempotency import (
    PatientNameValueError,
    normalize_patient_name,
)
from apps.tenancy.envelope import protect, reveal

if TYPE_CHECKING:
    from collections.abc import Sequence

INVALID_NAME_MESSAGE = "Enter a valid normalized patient name."
TEXT_TYPE_MESSAGE = "encrypted text requires a string"
DATE_TYPE_MESSAGE = "encrypted date requires a date value"
BYTES_TYPE_MESSAGE = "encrypted bytes require a bytes value"


def _normalized_name(value: str) -> str:
    try:
        return normalize_patient_name(value)
    except PatientNameValueError as error:
        raise ValidationError(
            INVALID_NAME_MESSAGE, code="invalid_patient_name"
        ) from error


if TYPE_CHECKING:

    class _EncryptedFieldBase(models.BinaryField[Any, Any]):
        pass

else:

    class _EncryptedFieldBase(models.BinaryField):
        pass


class EncryptedField(_EncryptedFieldBase):
    """Base bytea envelope field; subclasses define the value codec."""

    # ORM reads decrypt through from_db_value; the codec decides the type.
    _pyi_private_get_type: Any

    def __init__(self, *args: Any, purpose: str, **kwargs: Any) -> None:
        """Bind the envelope purpose this column encrypts under."""
        self.envelope_purpose = purpose
        super().__init__(*args, **kwargs)

    def deconstruct(self) -> tuple[str, str, Sequence[Any], dict[str, Any]]:
        """Carry the envelope purpose through migration serialization."""
        name, path, args, kwargs = super().deconstruct()
        kwargs["purpose"] = self.envelope_purpose
        return name, path, args, kwargs

    def _encode(self, value: Any) -> bytes | None:
        raise NotImplementedError

    def _decode(self, plaintext: bytes) -> Any:
        raise NotImplementedError

    def _empty_value(self) -> Any:
        return None

    def get_db_prep_value(
        self, value: Any, connection: object, prepared: bool = False
    ) -> bytes | None:
        """Encode the value and wrap it in a tenant envelope for storage."""
        del connection, prepared
        encoded = self._encode(value)
        if encoded is None:
            return None
        return protect(purpose=self.envelope_purpose, plaintext=encoded)

    def from_db_value(
        self,
        value: Any,
        expression: object,
        connection: object,
    ) -> Any:
        """Reveal the stored envelope and decode it to the field's type."""
        del expression, connection
        if value is None:
            return self._empty_value()
        plaintext = reveal(purpose=self.envelope_purpose, envelope=bytes(value))
        return self._decode(plaintext)

    def value_to_string(self, obj: models.Model) -> str:
        """Refuse serialization; protected fields have no supported path."""
        raise NotImplementedError


class EncryptedTextField(EncryptedField):
    """UTF-8 text envelope; the empty string is stored as NULL."""

    _pyi_private_get_type: str

    def _encode(self, value: Any) -> bytes | None:
        if value is None or value == "":
            return None
        if not isinstance(value, str):
            raise ValidationError(TEXT_TYPE_MESSAGE)
        return value.encode("utf-8")

    def _decode(self, plaintext: bytes) -> str:
        return plaintext.decode("utf-8")

    def _empty_value(self) -> str:
        return ""


class EncryptedPatientNameField(EncryptedTextField):
    """Normalized patient name stored only as a tenant envelope."""

    def to_python(self, value: object) -> str:
        """Normalize any assigned string to the canonical patient name."""
        if isinstance(value, str):
            return _normalized_name(value)
        if value is None:
            return ""
        raise ValidationError(INVALID_NAME_MESSAGE, code="invalid_patient_name")

    def pre_save(self, model_instance: models.Model, add: bool) -> str:
        """Normalize the instance value in place before it is persisted."""
        del add
        value = getattr(model_instance, self.attname, None)
        if not isinstance(value, str):
            raise ValidationError(INVALID_NAME_MESSAGE, code="invalid_patient_name")
        normalized = _normalized_name(value)
        setattr(model_instance, self.attname, normalized)
        return normalized


class EncryptedDateField(EncryptedField):
    """ISO-8601 date envelope; decryption returns a ``date``."""

    _pyi_private_get_type: date

    def _encode(self, value: Any) -> bytes | None:
        if value is None:
            return None
        if type(value) is not date:
            raise ValidationError(DATE_TYPE_MESSAGE)
        return value.isoformat().encode("ascii")

    def _decode(self, plaintext: bytes) -> date:
        return date.fromisoformat(plaintext.decode("ascii"))


class EncryptedJSONField(EncryptedField):
    """Canonical-JSON envelope; NULL decodes to the configured empty value."""

    def __init__(
        self, *args: Any, purpose: str, empty: Any = None, **kwargs: Any
    ) -> None:
        """Bind the envelope purpose and the value NULL decodes to."""
        self._empty = empty
        super().__init__(*args, purpose=purpose, **kwargs)

    def deconstruct(self) -> tuple[str, str, Sequence[Any], dict[str, Any]]:
        """Carry purpose and empty value through migration serialization."""
        name, path, args, kwargs = super().deconstruct()
        if self._empty is not None:
            kwargs["empty"] = self._empty
        return name, path, args, kwargs

    def _encode(self, value: Any) -> bytes | None:
        if value is None:
            return None
        return rfc8785.dumps(value)

    def _decode(self, plaintext: bytes) -> Any:
        return json.loads(plaintext.decode("utf-8"))

    def _empty_value(self) -> Any:
        return self._empty


class EncryptedBytesField(EncryptedField):
    """Raw byte envelope for binary payloads such as rendered documents."""

    _pyi_private_get_type: bytes

    def _encode(self, value: Any) -> bytes | None:
        if value is None:
            return None
        if isinstance(value, memoryview):
            value = bytes(value)
        if not isinstance(value, bytes):
            raise ValidationError(BYTES_TYPE_MESSAGE)
        return value

    def _decode(self, plaintext: bytes) -> bytes:
        return plaintext
