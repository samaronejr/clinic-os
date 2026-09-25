from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.scheduling.appointment_persistence import _constraint_name
from apps.scheduling.models import Appointment
from apps.scheduling.services import AvailabilityCreateInputError, create_availability
from apps.tenancy.db import tenant_context
from django.db import IntegrityError, transaction

from patient_service_support import runtime_role
from scheduling.appointment_service_support import seed_appointment_setup

if TYPE_CHECKING:
    from datetime import datetime

    from conftest import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_frozen_resource_clock_keeps_python_and_sql_future_guards(
    rbac_graph: RbacGraph, resource_clock: datetime
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        with pytest.raises(AvailabilityCreateInputError):
            create_availability(
                clinic_id=setup.clinic_id,
                practitioner_id=setup.practitioner_id,
                start_local="2035-05-31T08:00",
                end_local="2035-05-31T12:00",
                idempotency_key=uuid4(),
            )
        with pytest.raises(IntegrityError) as error, transaction.atomic():
            Appointment.objects.create(
                organization_id=setup.organization_id,
                clinic_id=setup.clinic_id,
                patient_id=setup.patient_id,
                practitioner_id=setup.practitioner_id,
                start_at=resource_clock - timedelta(hours=2),
                end_at=resource_clock - timedelta(hours=1),
                idempotency_key=uuid4(),
                create_fingerprint=b"s" * 32,
            )
        assert _constraint_name(error.value) == "scheduling_appointment_future_check"
        assert not Appointment.objects.exists()
