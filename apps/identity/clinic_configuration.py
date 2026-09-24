"""Bounded clinic settings; timezone and permission authority are not settings."""

from __future__ import annotations

import io
import re
import warnings
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import connection, transaction
from PIL import Image, UnidentifiedImageError

from apps.audit.services import record_phase1_event
from apps.ehr.attachment_scanner import AttachmentScanUnavailableError, default_scanner
from apps.ehr.attachments import AttachmentInput, detect_attachment_type
from apps.ehr.models import ClinicalAttachment
from apps.identity.current_context import require_current_actor_clinic_roles
from apps.identity.models import Clinic, ClinicConfiguration, UserClinicRole
from apps.identity.overlay_content import validate_overlay_text

if TYPE_CHECKING:
    from uuid import UUID

CONFIGURATION_ROLES = (UserClinicRole.Role.OWNER, UserClinicRole.Role.CLINIC_ADMIN)
REMINDER_HOURS = (1, 2, 6, 12, 24, 48, 72)
MAX_LOGO_BYTES = 256 * 1024
MAX_LOGO_DIMENSION = 1024
MAX_DISPLAY_NAME = 120
MAX_EMAIL = 254
DEFAULT_BRAND = "navy"


@dataclass(frozen=True)
class ConfigurationContent:
    """Closed, data-only fields; not a permission or stylesheet vocabulary."""

    display_name: str
    contact_email: str = ""
    contact_phone: str = ""
    brand_token: str = DEFAULT_BRAND
    reminder_hours: int = 24

    def validate(self) -> None:
        """Validate text, formats and fixed tokens at the service boundary."""
        validate_overlay_text(self.display_name)
        if (
            not self.display_name.strip()
            or len(self.display_name) > MAX_DISPLAY_NAME
            or self.brand_token not in ("navy", "teal")
            or type(self.reminder_hours) is not int
            or self.reminder_hours not in REMINDER_HOURS
            or len(self.contact_email) > MAX_EMAIL
            or (
                self.contact_phone
                and not re.fullmatch(r"\+[1-9][0-9]{7,14}", self.contact_phone)
            )
        ):
            message = "Configuração inválida. Use somente os valores aprovados."
            raise ValidationError(message)
        if self.contact_email:
            validate_email(self.contact_email)


def latest_configuration(clinic_id: UUID) -> ClinicConfiguration | None:
    """Read the current snapshot under the caller's existing clinic authority/RLS."""
    return (
        ClinicConfiguration.objects.filter(clinic_id=clinic_id)
        .order_by("-version")
        .first()
    )


def validated_logo(upload: AttachmentInput) -> bytes:
    """Apply attachment type/scan policy, then decode and strip raster metadata.

    Logos have a stricter bound than clinical attachments. No bytes are stored
    before a clean scan and successful decode. An unavailable production scanner
    fails closed; a synthetic verdict never enables production uploads.
    """
    if (
        not upload.data
        or len(upload.data) > MAX_LOGO_BYTES
        or upload.declared_type not in ("image/png", "image/jpeg")
        or detect_attachment_type(upload.data) != upload.declared_type
    ):
        msg = "Envie PNG ou JPEG válido de até 256 KiB."
        raise ValidationError(msg)
    try:
        verdict, _reason = default_scanner().scan(
            attachment=ClinicalAttachment(size_bytes=len(upload.data)), data=upload.data
        )
    except AttachmentScanUnavailableError as error:
        msg = "Verificação de arquivo indisponível; logo não alterado."
        raise ValidationError(msg) from error
    if verdict != "clean":
        msg = "Arquivo recusado pela política de anexos."
        raise ValidationError(msg)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(upload.data)) as image:
                if (
                    image.format not in ("PNG", "JPEG")
                    or max(image.size) > MAX_LOGO_DIMENSION
                    or getattr(image, "n_frames", 1) != 1
                ):
                    msg = "Use imagem estática de até 1024 x 1024 pixels."
                    raise ValidationError(msg)
                image.load()
                output = io.BytesIO()
                image.convert("RGBA").save(output, format="PNG")
    except (
        UnidentifiedImageError,
        OSError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as error:
        msg = "Imagem inválida."
        raise ValidationError(msg) from error
    result = output.getvalue()
    if len(result) > MAX_LOGO_BYTES:
        msg = "Imagem normalizada excede 256 KiB."
        raise ValidationError(msg)
    return result


def publish_configuration(
    *,
    clinic_id: UUID,
    expected_version: int,
    content: ConfigurationContent,
    logo: AttachmentInput | None = None,
    remove_logo: bool = False,
) -> ClinicConfiguration:
    """Publish one serialized snapshot; existing schedules and artifacts stay intact."""
    actor = require_current_actor_clinic_roles(clinic_id, CONFIGURATION_ROLES)
    content.validate()
    if (
        type(expected_version) is not int
        or expected_version < 0
        or (logo is not None and remove_logo)
    ):
        message = "Versão ou operação de logo inválida."
        raise ValidationError(message)
    logo_bytes = validated_logo(logo) if logo is not None else None
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            [f"clinic-configuration:{clinic_id}"],
        )
        previous = latest_configuration(clinic_id)
        if expected_version != (previous.version if previous else 0):
            msg = "A configuração mudou. Recarregue antes de editar."
            raise ValidationError(msg)
        clinic = Clinic.objects.get(pk=clinic_id)
        configuration = ClinicConfiguration.objects.create(
            clinic=clinic,
            organization_id=clinic.organization_id,
            version=expected_version + 1,
            **asdict(content),
            logo_png=(
                logo_bytes
                if logo_bytes is not None
                else bytes(previous.logo_png)
                if previous and not remove_logo
                else b""
            ),
            published_by_id=actor,
        )
        record_phase1_event(
            "identity.clinic_configuration.published",
            clinic_id=clinic_id,
            affected_record_id=configuration.pk,
        )
        return configuration
