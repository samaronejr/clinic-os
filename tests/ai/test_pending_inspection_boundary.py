"""Finance assertions use the real, unprivileged reader, absent from the image."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from apps.ai.models import AIArtifact
from apps.ai.pending_access import PendingReadOutsideVisibilityError
from apps.ai.pending_outputs import key
from ops.testing.image_source import candidate_context_target

from ai import pending_observation
from ai.revenue_support import request_and_collect
from ai.test_revenue_cycle import finance_setup, world
from billing.journal_support import finance_scope

if TYPE_CHECKING:
    from ai.revenue_support import RevenueWorld

__all__ = ("finance_setup", "world")


def test_finance_inspection_is_absent_from_the_application_import_path() -> None:
    root = Path(__file__).resolve().parents[2]
    helper = Path(pending_observation.__file__).resolve().relative_to(root)
    assert helper.as_posix() == "tests/ai/pending_observation.py"
    assert candidate_context_target("application", helper.as_posix()) is None
    assert candidate_context_target("application", "apps/ai/pending_outputs.py")
    product = [
        p
        for folder in ("apps", "config", "ops")
        for p in (root / folder).rglob("*.py")
        if candidate_context_target("application", p.relative_to(root).as_posix())
    ]
    assert product
    assert not [
        p.relative_to(root)
        for p in [*product, root / "manage.py", root / "sitecustomize.py"]
        if "pending_observation" in p.read_text() or p.stem == "pending_observation"
    ]


@pytest.mark.django_db(transaction=True)
def test_finance_inspection_cannot_reauthorize_a_retained_or_foreign_row(
    world: RevenueWorld,
) -> None:
    review = request_and_collect(world)
    assert review.output_id is not None
    setup = world.setup
    with finance_scope(setup):
        raw = AIArtifact.objects.get(pk=review.output_id)
        selector = key(raw)
        assert (
            pending_observation.checked_value(
                selector, setup.clinic_id, lambda r: r.kind
            )
            == "output"
        )
        with pytest.raises(PendingReadOutsideVisibilityError):
            _ = raw.kind
        with pytest.raises(AssertionError, match="row not visible"):
            pending_observation.checked_value(
                selector, setup.graph.clinic_b, lambda r: r.kind
            )
        with pytest.raises(PendingReadOutsideVisibilityError):
            _ = raw.kind
