"""Translate validated Settings forms into the existing typed service inputs."""

from __future__ import annotations

from typing import TYPE_CHECKING, assert_never

from apps.scheduling.resource_forms import GenerationInput, RetirementInput
from apps.scheduling.resource_services import (
    ClosureInput,
    ResourceInput,
    ServiceInput,
    TemplateInput,
    create_closure,
    create_resource,
    create_service_type,
    create_template,
    generate_availability,
    retire_definition,
)

if TYPE_CHECKING:
    from uuid import UUID

    from apps.scheduling.resource_forms import DefinitionForm


def publish_definition(*, clinic_id: UUID, form: DefinitionForm) -> None:
    """Publish only the typed operation represented by the validated form."""
    definition = form.definition()
    match definition:
        case ResourceInput():
            create_resource(clinic_id=clinic_id, content=definition)
        case ServiceInput():
            create_service_type(clinic_id=clinic_id, content=definition)
        case TemplateInput():
            create_template(clinic_id=clinic_id, content=definition)
        case ClosureInput():
            create_closure(clinic_id=clinic_id, content=definition)
        case GenerationInput():
            generate_availability(
                clinic_id=clinic_id,
                template_id=definition.template_id,
                start_date=definition.start_date,
                end_date=definition.end_date,
            )
        case RetirementInput():
            retire_definition(
                clinic_id=clinic_id,
                kind=definition.kind,
                record_id=definition.record_id,
            )
        case unexpected:
            assert_never(unexpected)
