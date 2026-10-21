"""Clinic-owned original templates; no normal findings or instrument wording."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

from django.utils.translation import gettext_lazy as _

if TYPE_CHECKING:
    from django_stubs_ext import StrOrPromise


@dataclass(frozen=True, slots=True)
class PackDefinition:
    """A release pins content, calculator versions, license evidence and order."""

    key: str
    version: int
    title: StrOrPromise
    fields: tuple[tuple[str, str, StrOrPromise], ...]
    calculators: tuple[str, ...]
    questionnaire_label: StrOrPromise


PACKS: Final = MappingProxyType(
    {
        "general": PackDefinition(
            "general",
            1,
            _("General practice"),
            (
                ("reason", "subjective", _("Reason for consultation")),
                ("history", "subjective", _("Relevant history")),
                ("findings", "objective", _("Observed findings")),
            ),
            ("bmi", "egfr"),
            _("Information you want the clinician to review"),
        ),
        "pediatrics": PackDefinition(
            "pediatrics",
            1,
            _("Pediatrics"),
            (
                ("growth", "objective", _("Growth measurements and their source")),
                ("immunizations", "subjective", _("Reported immunization history")),
            ),
            ("corrected_age", "growth_percentile"),
            _("Immunizations and dates documented in your vaccination record"),
        ),
        "psychiatry": PackDefinition(
            "psychiatry",
            1,
            _("Psychiatry"),
            tuple(
                (key, "objective", label)
                for key, label in (
                    ("appearance", _("Appearance and behavior")),
                    ("speech", _("Speech")),
                    ("mood", _("Mood and affect")),
                    ("thought", _("Thought form and content")),
                    ("perception", _("Perception")),
                    ("cognition", _("Cognition")),
                    ("insight", _("Insight and judgment")),
                )
            ),
            ("phq9", "gad7"),
            _("Concerns you want to discuss privately"),
        ),
        "procedures": PackDefinition(
            "procedures",
            1,
            _("Procedures"),
            (
                ("procedure", "plan", _("Procedure plan")),
                ("consent", "plan", _("Consent document and discussion")),
                ("images", "objective", _("Images attached to this encounter")),
            ),
            (),
            _("Questions about the planned procedure"),
        ),
    }
)
LICENSE_REF: Final = "docs/clinical/specialty-packs.md"


def template_definition(pack: PackDefinition) -> dict[str, object]:
    """Prompts ask for recorded facts, never insert answers or defaults."""
    return {
        **dict.fromkeys(("subjective", "objective", "assessment", "plan"), ""),
        "schema_version": 2,
        "state": "published",
        "optional_sections": [],
        "phrases": [],
        "fields": [
            {
                "id": key,
                "section": section,
                "label": str(label),
                "type": "text",
                "required": False,
                "max_length": 4000,
            }
            for key, section, label in pack.fields
        ]
        + [
            {
                "id": f"{key}_score",
                "section": "objective",
                "label": "PHQ-9" if key == "phq9" else "GAD-7",
                "type": "scale-ref",
                "required": False,
                "scale_ref": key,
                "scale_version": 1,
            }
            for key in pack.calculators
            if key in {"phq9", "gad7"}
        ],
    }


def questionnaire_definition(pack: PackDefinition) -> list[dict[str, object]]:
    """One optional narrative is not a screening instrument or a scored scale."""
    return [
        {
            "id": "q_history",
            "label": str(pack.questionnaire_label),
            "type": "text",
            "required": False,
            "max_length": 4000,
            "options": [],
        }
    ]
