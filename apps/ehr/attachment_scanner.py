"""Scanner boundary for quarantined attachments.

The synthetic scanner inspects bytes already materialized inside the tenant
transaction and returns a fixed verdict vocabulary. It exists so the
quarantine lifecycle is exercisable end to end; it is not a security product
and can never approve production uploads. Real use requires an approved
scanning capability wired behind this protocol.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from django.conf import settings

if TYPE_CHECKING:
    from apps.ehr.models import ClinicalAttachment

# Marker substrings the synthetic scanner treats as active or embedded content.
ACTIVE_MARKERS: tuple[tuple[bytes, str], ...] = (
    (b"<script", "active_markup"),
    (b"<?xml", "active_markup"),
    (b"<svg", "active_markup"),
    (b"<html", "active_markup"),
    (b"<!doctype html", "active_markup"),
    (b"/JavaScript", "active_pdf_content"),
    (b"/JS/", "active_pdf_content"),
    (b"/OpenAction", "active_pdf_content"),
    (b"/Launch", "active_pdf_content"),
    (b"/EmbeddedFile", "embedded_file"),
    (b"/RichMedia", "embedded_file"),
    (b"PK\x03\x04", "embedded_archive"),
    (b"\x1f\x8b\x08", "embedded_archive"),
    (b"Rar!", "embedded_archive"),
)


class AttachmentScanUnavailableError(Exception):
    """Report that no approved scanning capability is configured."""


class AttachmentScanner(Protocol):
    """Classify one quarantined object inside the tenant boundary."""

    def scan(self, *, attachment: ClinicalAttachment, data: bytes) -> tuple[str, str]:
        """Return ``(verdict, reason)``; verdict is ``clean`` or ``rejected``."""
        ...


class SyntheticAttachmentScanner:
    """Marker-based synthetic verdicts; never an approval for real uploads."""

    def scan(self, *, attachment: ClinicalAttachment, data: bytes) -> tuple[str, str]:
        """Reject active or embedded content; otherwise report the object clean."""
        if settings.CLINIC_DATA_MODE != "synthetic":
            raise AttachmentScanUnavailableError
        if len(data) != attachment.size_bytes:
            return "rejected", "truncated_object"
        lowered = data.lower()
        for marker, reason in ACTIVE_MARKERS:
            if marker.lower() in lowered:
                return "rejected", reason
        return "clean", ""


def default_scanner() -> AttachmentScanner:
    """Resolve the configured scanner; synthetic deployments get the stub."""
    return SyntheticAttachmentScanner()
