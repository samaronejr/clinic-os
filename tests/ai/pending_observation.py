"""Obtain fixture observations through the same visibility checks as readers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from apps.ai import pending_outputs
from apps.ai.pending_access import workflow
from apps.consent.decision_fence import decision_fenced
from django.db import models

if TYPE_CHECKING:
    from collections.abc import Callable
    from uuid import UUID


def checked_value[M: models.Model, V](
    selector: pending_outputs.PendingKey[M], clinic: UUID, read: Callable[[M], V]
) -> V:
    """Use the production reader on the caller's connection, even before a fence."""

    def project(batch: pending_outputs.RenderSelection) -> V:
        checked = batch.get(selector)
        assert checked is not None, "row not visible"
        return read(checked.record)

    return pending_outputs.ai_pending_visible((selector,), project, clinic_id=clinic)


def observe[M: models.Model, V](row: M, clinic: UUID, read: Callable[[M], V]) -> V:
    if not decision_fenced():
        # Preserve the original pre-admission observation; racing a withdrawal
        # against a later decision must not acquire a lock on its behalf.
        return read(row)
    return checked_value(pending_outputs.key(row), clinic, read)


def count_rows[M: models.Model](model: type[M], clinic: UUID, **filters: object) -> int:
    with workflow(clinic):
        return pending_outputs.rows(model).filter(**filters).count()
