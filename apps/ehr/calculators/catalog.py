"""Closed calculator registry; clinical licensing cannot be claimed by a caller."""

from types import MappingProxyType
from typing import Final

from django.utils.translation import gettext_noop

from apps.ehr.calculators.bmi import SOURCE as BMI_SOURCE
from apps.ehr.calculators.contracts import CalculatorDefinition, CalculatorInput
from apps.ehr.calculators.corrected_age import SOURCE as CORRECTED_AGE_SOURCE
from apps.ehr.calculators.egfr import SOURCE as EGFR_SOURCE
from apps.ehr.calculators.gad7 import ITEMS as GAD7_ITEMS
from apps.ehr.calculators.phq9 import ITEMS as PHQ9_ITEMS
from apps.ehr.calculators.phq9 import SOURCE as PHQ9_SOURCE
from apps.ehr.calculators.vectors import VECTORS

LICENSE_REF: Final = "docs/clinical/specialty-packs.md#sources-and-permissions"
DEFINITIONS: Final = MappingProxyType(
    {
        "bmi": CalculatorDefinition(
            "bmi",
            1,
            "Body mass index",
            BMI_SOURCE,
            "published_formula",
            LICENSE_REF,
            (
                CalculatorInput("weight", "Weight", gettext_noop("kg or g")),
                CalculatorInput("weight_unit", "Weight unit", "", ("kg", "g")),
                CalculatorInput("height", "Height", gettext_noop("cm or m")),
                CalculatorInput("height_unit", "Height unit", "", ("cm", "m")),
            ),
            VECTORS["bmi"],
        ),
        "egfr": CalculatorDefinition(
            "egfr",
            1,
            "Estimated glomerular filtration rate",
            EGFR_SOURCE,
            "published_formula",
            LICENSE_REF,
            (
                CalculatorInput("age", "Completed age", "a"),
                CalculatorInput(
                    "sex", "Sex used by the published equation", "", ("female", "male")
                ),
                CalculatorInput(
                    "creatinine", "Serum creatinine", gettext_noop("mg/dL or umol/L")
                ),
                CalculatorInput(
                    "creatinine_unit", "Creatinine unit", "", ("mg/dL", "umol/L")
                ),
            ),
            VECTORS["egfr"],
        ),
        "corrected_age": CalculatorDefinition(
            "corrected_age",
            1,
            "Corrected age",
            CORRECTED_AGE_SOURCE,
            "published_formula",
            LICENSE_REF,
            (
                CalculatorInput("chronological_age", "Chronological age", "wk"),
                CalculatorInput("gestational_age", "Gestational age at birth", "wk"),
            ),
            VECTORS["corrected_age"],
        ),
        "growth_percentile": CalculatorDefinition(
            "growth_percentile",
            1,
            "Growth percentiles",
            "EG-13",
            "pending",
            LICENSE_REF,
            (),
            VECTORS["growth_percentile"],
        ),
        **{
            key: CalculatorDefinition(
                key,
                1,
                title,
                PHQ9_SOURCE,
                "publisher_free_use",
                LICENSE_REF,
                tuple(
                    CalculatorInput(item, "Item score", "1", ("0", "1", "2", "3"))
                    for item in items
                ),
                VECTORS[key],
            )
            for key, title, items in (
                ("phq9", "PHQ-9", PHQ9_ITEMS),
                ("gad7", "GAD-7", GAD7_ITEMS),
            )
        },
    }
)
