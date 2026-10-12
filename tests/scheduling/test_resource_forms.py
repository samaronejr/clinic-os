from __future__ import annotations

from datetime import date, time
from typing import TYPE_CHECKING
from uuid import UUID

import pytest
from apps.scheduling.resource_forms import (
    ClosureForm,
    GenerateForm,
    GenerationInput,
    ResourceForm,
    RetireForm,
    RetirementInput,
    ServiceForm,
    TemplateForm,
)
from apps.scheduling.resource_services import (
    ClosureInput,
    ResourceInput,
    ServiceInput,
    TemplateInput,
)
from django.http import QueryDict

if TYPE_CHECKING:
    from collections.abc import Callable

    from apps.scheduling.resource_forms import DefinitionForm

type PublicationInput = (
    ResourceInput
    | ServiceInput
    | TemplateInput
    | ClosureInput
    | GenerationInput
    | RetirementInput
)

SUBJECT = UUID("00000000-0000-4000-8000-000000000041")


@pytest.mark.parametrize(
    ("factory", "expected"),
    [
        (
            lambda: ResourceForm({"name": "R2 room", "kind": "room", "capacity": "3"}),
            ResourceInput(name="R2 room", kind="room", capacity=3),
        ),
        (
            lambda: ServiceForm(
                {
                    "name": "R2 service",
                    "duration_min": "45",
                    "buffer_before": "7",
                    "buffer_after": "11",
                    "required_professional_roles": ["nurse", "physician"],
                    "required_resource_kinds": ["location", "room"],
                    "insurer_billable": "on",
                    "price_ref": "SYNTHETIC-PRICE-2",
                }
            ),
            ServiceInput(
                name="R2 service",
                duration_min=45,
                buffer_before=7,
                buffer_after=11,
                required_professional_roles=("nurse", "physician"),
                required_resource_kinds=("location", "room"),
                insurer_billable=True,
                price_ref="SYNTHETIC-PRICE-2",
            ),
        ),
        (
            lambda: TemplateForm(
                QueryDict(
                    f"resource_id={SUBJECT}&weekdays=1&weekdays=3"
                    "&start_local=08%3A15&end_local=10%3A45"
                    "&valid_from=2035-06-02&valid_to=2035-06-09"
                ),
                practitioners=[],
                resources=[(str(SUBJECT), "R2 room")],
            ),
            TemplateInput(
                resource_id=SUBJECT,
                weekdays=(1, 3),
                start_local=time(8, 15),
                end_local=time(10, 45),
                valid_from=date(2035, 6, 2),
                valid_to=date(2035, 6, 9),
            ),
        ),
        (
            lambda: ClosureForm(
                QueryDict(
                    "start_local=2035-06-02T11%3A00"
                    "&end_local=2035-06-02T12%3A00&reason=holiday"
                ),
                practitioners=[],
                resources=[],
            ),
            ClosureInput(
                start_local="2035-06-02T11:00",
                end_local="2035-06-02T12:00",
                reason="holiday",
            ),
        ),
        (
            lambda: GenerateForm(
                {
                    "template_id": str(SUBJECT),
                    "start_date": "2035-06-02",
                    "end_date": "2035-06-09",
                }
            ),
            GenerationInput(
                template_id=SUBJECT,
                start_date=date(2035, 6, 2),
                end_date=date(2035, 6, 9),
            ),
        ),
        (
            lambda: RetireForm({"kind": "template", "record_id": str(SUBJECT)}),
            RetirementInput(kind="template", record_id=SUBJECT),
        ),
    ],
    ids=("resource", "service", "template", "closure", "generation", "retirement"),
)
def test_definition_keeps_validated_field_types(
    factory: Callable[[], DefinitionForm],
    expected: PublicationInput,
) -> None:
    # Given a valid native form, including values different from its defaults.
    form = factory()
    assert form.is_valid(), form.errors

    # When the boundary produces the domain operation.
    actual = form.definition()

    # Then UUID/date/time/checkbox/list coercions and selected values survive.
    assert actual == expected
