"""Bounded source evidence and exact, non-inferential result values."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Final

import rfc8785
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

if TYPE_CHECKING:
    from datetime import date
    from uuid import UUID

CATALOG: Final = {
    "cbc": _("Complete blood count"),
    "urinalysis": _("Urinalysis"),
}
INVALID = _("Check the result source, identity factors and observations.")
DISPOSITIONS: Final = ("no_action", "follow_up", "urgent_review")
REJECTION_CODES: Final = ("wrong_patient_confirmed", "invalid_source_confirmed")
MAX_SOURCE_TEXT: Final = 256
MAX_SOURCE_VALUE: Final = 128
MAX_SOURCE_UNIT: Final = 64
MAX_OBSERVATIONS: Final = 128


class OrdersAccessDeniedError(Exception):
    """Unknown and foreign records have exactly the same payload-free denial."""

    def __init__(self) -> None:
        """Keep selectors and clinical content out of exception text."""
        super().__init__("current actor unauthorized")


class OrdersConflictError(Exception):
    """A stale source or altered replay never mutates current review state."""

    def __init__(self, code: str = "result_conflict") -> None:
        """Expose only a closed machine error code."""
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True, kw_only=True)
class IdentityEvidence:
    """DOB plus exact name or identifier; omitted evidence is never guessed."""

    birth_date: date | None = None
    name: str = ""
    identifier_kind: str = ""
    identifier: str = ""

    def payload(self) -> dict[str, str]:
        """Normalize only whitespace and Unicode/case, never edit distance."""
        return {
            "birth_date": self.birth_date.isoformat() if self.birth_date else "",
            "name": " ".join(unicodedata.normalize("NFC", self.name).split()).lower(),
            "identifier_kind": self.identifier_kind,
            "identifier": self.identifier,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class ObservationInput:
    """Source text remains exact, even when a separator or unit is ambiguous."""

    code: str
    value: str
    unit: str
    reference_interval: str = ""
    abnormal_flag: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class ResultInput:
    """Immutable submission terms; a correction names its exact predecessor."""

    order_id: UUID | None
    identity: IdentityEvidence
    content_type: str
    data: bytes
    event_id: UUID
    observations: tuple[ObservationInput, ...] = ()
    critical: bool = False
    result_id: UUID | None = None
    predecessor_id: UUID | None = None

    def fingerprint(self) -> str:
        """Bind content and all semantic terms, excluding random storage keys."""
        payload = {
            "order": str(self.order_id) if self.order_id else None,
            "identity": self.identity.payload(),
            "type": self.content_type,
            "sha256": hashlib.sha256(self.data).hexdigest(),
            "observations": [asdict(value) for value in self.observations],
            "critical": self.critical,
            "result": str(self.result_id) if self.result_id else None,
            "predecessor": str(self.predecessor_id) if self.predecessor_id else None,
        }
        return hashlib.sha256(rfc8785.dumps(payload)).hexdigest()


@dataclass(frozen=True, slots=True)
class IngestReceipt:
    """A response reveals neither candidate identity nor a storage locator."""

    result_id: UUID
    version_id: UUID
    number: int
    state: str


def validate_submission(submission: ResultInput) -> None:
    """Bound untrusted facts without interpreting values, flags or reference ranges."""
    identity = submission.identity.payload()
    if (
        any(len(value) > MAX_SOURCE_TEXT for value in identity.values())
        or len(submission.observations) > MAX_OBSERVATIONS
        or type(submission.critical) is not bool
        or ((submission.result_id is None) != (submission.predecessor_id is None))
    ):
        raise ValidationError(INVALID)
    for observation in submission.observations:
        if (
            re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", observation.code) is None
            or not observation.value
            or len(observation.value) > MAX_SOURCE_VALUE
            or len(observation.unit) > MAX_SOURCE_UNIT
            or len(observation.reference_interval) > MAX_SOURCE_TEXT
            or observation.abnormal_flag
            not in ("", "normal", "low", "high", "critical")
        ):
            raise ValidationError(INVALID)
