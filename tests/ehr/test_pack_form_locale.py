"""Calculator labels use the shipped locale while posted units stay canonical."""

import pytest
from apps.ehr.calculators.catalog import DEFINITIONS
from apps.ehr.pack_forms import calculator_form
from django.utils.translation import gettext, override


@pytest.mark.parametrize(
    ("key", "field"), [("bmi", "weight"), ("bmi", "height"), ("egfr", "creatinine")]
)
@pytest.mark.parametrize("language", ["en", "pt-br"])
def test_alternative_unit_label_matches_the_shipped_catalog(
    key: str, field: str, language: str
) -> None:
    definition = DEFINITIONS[key]
    spec = next(item for item in definition.inputs if item.key == field)
    with override(language):
        form = calculator_form(definition)
        translated = gettext(spec.unit)
        if language == "pt-br":
            assert translated != spec.unit
        assert form[field].label == f"{gettext(spec.label)} ({translated})"
        assert f"({translated})" in str(form[field].label_tag())
        for item in definition.inputs:
            if item.choices:
                assert [widget.data["value"] for widget in form[item.key]] == [
                    "",
                    *item.choices,
                ]
