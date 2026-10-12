"""Stable public keyword-only resource services and immutable input types."""

from apps.scheduling.availability_generation import (
    MAX_GENERATION_DAYS,
    generate_availability,
    require_open_window,
)
from apps.scheduling.availability_publication import (
    LAST_WEEKDAY,
    create_closure,
    create_template,
    retire_definition,
)
from apps.scheduling.resource_definitions import (
    MAX_BUFFER,
    MAX_CAPACITY,
    MAX_DURATION,
    MAX_NAME,
    MAX_PRICE_REF,
    ClosureInput,
    ResourceInput,
    ServiceInput,
    TemplateInput,
    _subject,
    configuration_clinic,
    create_resource,
    create_service_type,
)

__all__ = (
    "LAST_WEEKDAY",
    "MAX_BUFFER",
    "MAX_CAPACITY",
    "MAX_DURATION",
    "MAX_GENERATION_DAYS",
    "MAX_NAME",
    "MAX_PRICE_REF",
    "ClosureInput",
    "ResourceInput",
    "ServiceInput",
    "TemplateInput",
    "_subject",
    "configuration_clinic",
    "create_closure",
    "create_resource",
    "create_service_type",
    "create_template",
    "generate_availability",
    "require_open_window",
    "retire_definition",
)
