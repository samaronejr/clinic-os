from importlib import import_module
from importlib.util import find_spec

from apps.tenancy.models import TenantScopedModel
from django.db import models


def test_availability_block_is_an_immutable_tenant_scoped_promise() -> None:
    module = (
        import_module("apps.scheduling.models")
        if find_spec("apps.scheduling.models")
        else None
    )

    assert module is not None
    availability = module.AvailabilityBlock
    assert issubclass(availability, TenantScopedModel)
    assert {field.name for field in availability._meta.fields} == {
        "clinic",
        "create_fingerprint",
        "created_at",
        "end_at",
        "generated_date",
        "id",
        "idempotency_key",
        "organization",
        "practitioner",
        "resource",
        "retired_at",
        "start_at",
        "template",
        "updated_at",
    }
    assert (
        availability._meta.get_field("clinic").remote_field.on_delete is models.PROTECT
    )
    assert (
        availability._meta.get_field("practitioner").remote_field.on_delete
        is models.PROTECT
    )
    assert (
        availability._meta.get_field("resource").remote_field.on_delete
        is models.PROTECT
    )
    assert (
        availability._meta.get_field("template").remote_field.on_delete
        is models.PROTECT
    )
    assert availability._meta.get_field("practitioner").null is True
    assert availability._meta.get_field("create_fingerprint").editable is False
    assert availability._meta.get_field("retired_at").null is True
