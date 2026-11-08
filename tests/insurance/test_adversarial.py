from __future__ import annotations

from hashlib import sha256
from typing import TYPE_CHECKING
from uuid import uuid4
from xml.etree.ElementTree import tostring

import pytest
from apps.audit.models import AuditEvent
from apps.insurance import services
from apps.insurance.access import fingerprint
from apps.insurance.errors import InsuranceConflictError
from apps.insurance.guides import stored_facts
from apps.insurance.models import Batch, Submission
from apps.insurance.tiss import (
    NS,
    PINNED_VERSIONS,
    TissValidationError,
    _resource,
    generate_batch,
)
from apps.insurance.tiss_types import BatchFacts
from django.utils import timezone
from django.utils.translation import gettext

from billing.journal_support import finance_scope
from identity.permission_support import owner_context

if TYPE_CHECKING:
    from insurance.cycle_support import Cycle

pytestmark = pytest.mark.django_db(transaction=True)


def test_invalid_xsd_cannot_be_sealed_or_expose_source_values(cycle: Cycle) -> None:
    clinic = cycle.finance.clinic_id
    with finance_scope(cycle.finance):
        successor = services.resubmit_guide(
            clinic_id=clinic, guide_id=cycle.guide.pk, command_id=uuid4()
        )
        services.validate_guide(
            clinic_id=clinic, guide_id=successor.pk, command_id=uuid4()
        )
        valid = generate_batch(
            facts=BatchFacts("2", timezone.now(), (stored_facts(successor),)),
            versions=PINNED_VERSIONS,
        )
        document = _resource(valid).root
        field = document.find(f".//{{{NS}}}CNES")
        assert field is not None
        field.text = "SINTETICO-SENTINELA-T59"
        invalid = tostring(document, encoding="utf-8", xml_declaration=True)
        batch = Batch.objects.create(
            organization_id=cycle.finance.graph.organization_a,
            clinic_id=clinic,
            command_id=uuid4(),
            fingerprint=fingerprint(["invalid-xsd", str(successor.pk)]),
            contract_id=successor.contract_id,
            number="2",
            guide_ids=[str(successor.pk)],
            versions=dict(PINNED_VERSIONS),
            xml=invalid.decode(),
            content_sha256=sha256(invalid).hexdigest(),
        )
        with pytest.raises(TissValidationError) as caught:
            services.seal_batch(clinic_id=clinic, batch_id=batch.pk, command_id=uuid4())
        batch.refresh_from_db()
        assert batch.state == "draft"
        assert caught.value.fields
        assert all(
            error.message == gettext("Invalid TISS field.")
            for error in caught.value.fields
        )
        assert "SINTETICO-SENTINELA-T59" not in str(caught.value.fields)
        assert not Submission.objects.filter(batch=batch).exists()
    with owner_context(cycle.finance.graph.organization_a):
        payloads = list(AuditEvent.objects.values_list("payload", flat=True))
        assert "SINTETICO-SENTINELA-T59" not in str(payloads)


def test_unknown_delivery_cannot_start_an_automatic_resubmission(cycle: Cycle) -> None:
    clinic = cycle.finance.clinic_id
    with finance_scope(cycle.finance):
        successor = services.resubmit_guide(
            clinic_id=clinic, guide_id=cycle.guide.pk, command_id=uuid4()
        )
        services.validate_guide(
            clinic_id=clinic, guide_id=successor.pk, command_id=uuid4()
        )
        batch = services.create_batch(
            clinic_id=clinic,
            contract_id=successor.contract_id,
            guide_ids=(successor.pk,),
            command_id=uuid4(),
        )
        services.validate_batch(clinic_id=clinic, batch_id=batch.pk, command_id=uuid4())
        services.seal_batch(clinic_id=clinic, batch_id=batch.pk, command_id=uuid4())
        services.prepare_submission(
            clinic_id=clinic, batch_id=batch.pk, channel="portal", command_id=uuid4()
        )
        services.submission_outcome(
            clinic_id=clinic, batch_id=batch.pk, outcome="unknown", command_id=uuid4()
        )
        with pytest.raises(InsuranceConflictError):
            services.prepare_submission(
                clinic_id=clinic,
                batch_id=batch.pk,
                channel="portal",
                command_id=uuid4(),
            )
        batch.refresh_from_db()
        assert batch.state == "unknown"
        assert Submission.objects.filter(batch=batch).count() == 1
