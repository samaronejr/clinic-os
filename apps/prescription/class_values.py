"""Manual prescription classes; no medication or provider can choose one."""

from typing import Final

from django.db import models
from django.utils.translation import gettext_lazy as _


class PrescriptionClass(models.TextChoices):
    """Closed taxonomy adopted by SD-6, with the separate rehearsal retained."""

    SIMPLE = "simple", _("Simple prescription")
    CONTROLE_ESPECIAL = "controle_especial", _("Special-control prescription")
    ANTIMICROBIAL = "antimicrobial_retention", _("Antimicrobial retention")
    GLP1 = "glp1_retention", _("GLP-1 retention")
    NOTIFICACAO_A = "notificacao_a", _("Notification A")
    NOTIFICACAO_B = "notificacao_b", _("Notification B")
    NOTIFICACAO_B2 = "notificacao_b2", _("Notification B2")
    RETINOID = "retinoid_systemic", _("Systemic retinoid")
    THALIDOMIDE = "thalidomide", _("Thalidomide")
    SYNTHETIC = "synthetic_non_controlled", _("Synthetic rehearsal; no real issuance")


CLASSES_CONTRACT: Final = "prescription-classes-v1"
REAL_CLASSES: Final = tuple(
    category
    for category in PrescriptionClass.values
    if category != PrescriptionClass.SYNTHETIC
)
