"""A retained extraction candidate cannot borrow a later rollout permission."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

import pytest
from apps.ai.contracts import GatewayAccessDeniedError, GatewayRefusedError
from apps.ai.extraction import review_extraction
from apps.orders.models import ResultObservation
from django.db import DatabaseError, transaction

from ai.retained_rollout_support import INVALIDATIONS, Invalidation, invalidate
from ai.test_extraction_filing import raw_confirmation
from ai.test_extraction_runtime import extracted_job
from ehr.test_autosave import as_actor
from identity.permission_support import owner_context

if TYPE_CHECKING:
    from uuid import UUID

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("mode", INVALIDATIONS)
@pytest.mark.parametrize("native", [False, True], ids=["python", "native"])
def test_retained_extraction_cannot_file_after_rollout_invalidation(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
    mode: Invalidation,
    native: bool,
) -> None:
    job = extracted_job(rbac_graph, monkeypatch)
    assert job.result_version_id is not None
    identifier: UUID | None
    with as_actor(rbac_graph, rbac_graph.physician):
        row = job.candidates.get(position=0)
        with transaction.atomic():
            if native:
                identifier, created = raw_confirmation(row, job.result_version_id)
                assert created
            else:
                identifier = review_extraction(
                    clinic_id=job.clinic_id,
                    candidate_id=row.pk,
                    decision="confirmed",
                )
            assert identifier is not None
            assert ResultObservation.objects.filter(pk=identifier).exists()
            transaction.set_rollback(True)
    clinic_id = invalidate(rbac_graph, job.invocation_id, mode, monkeypatch)
    with as_actor(rbac_graph, rbac_graph.physician):
        refused = (
            (DatabaseError,)
            if native
            else (
                GatewayAccessDeniedError,
                GatewayRefusedError,
            )
        )
        attempt = (
            partial(raw_confirmation, row, job.result_version_id, clinic_id=clinic_id)
            if native
            else partial(
                review_extraction,
                clinic_id=clinic_id,
                candidate_id=row.pk,
                decision="confirmed",
            )
        )
        with pytest.raises(refused), transaction.atomic():
            attempt()
    with owner_context(rbac_graph.organization_a):
        row.refresh_from_db()
        assert row.status == "candidate"
        assert row.observation_id is None
        assert not ResultObservation.objects.filter(
            version_id=job.result_version_id
        ).exists()
