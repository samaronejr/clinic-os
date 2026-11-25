"""Exercise the data boundary, not a list of safe serializer declarations."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from apps.ai import pending_outputs
from apps.ai.models import AIArtifact, AIInvocation
from apps.consent.decision_fence import try_hold_decision
from django.db.models import prefetch_related_objects
from django.template import Context, Template
from rest_framework import serializers

from ai.revenue_support import request_and_collect
from ai.test_revenue_cycle import finance_setup, world
from billing.journal_support import finance_scope

if TYPE_CHECKING:
    from collections.abc import Callable

    from ai.revenue_support import RevenueWorld

__all__ = ("finance_setup", "world")
pytestmark = pytest.mark.django_db(transaction=True)


class Metadata(serializers.Serializer[AIInvocation]):
    category = serializers.SerializerMethodField()

    def get_category(self, obj: AIInvocation) -> str:
        return obj.output.kind


class Custom(serializers.Serializer[AIInvocation]):
    def to_representation(self, instance: AIInvocation) -> dict[str, str]:
        return {"category": instance.output.kind}


@pytest.mark.parametrize(
    "route",
    [
        "method-field",
        "representation",
        "template",
        "values",
        "prefetch",
        "default-manager",
        "base-manager",
        "raw-attribute",
        "instance-dictionary",
        "cached-forward",
        "cached-reverse",
        "values-list",
        "joined-column",
        "cached-values",
    ],
)
def test_unwrapped_read_is_refused_under_the_consent_lock(
    world: RevenueWorld, route: str
) -> None:
    review = request_and_collect(world)
    assert review.output_id is not None
    assert review.invocation_id is not None
    with finance_scope(world.setup):
        raw = AIArtifact.objects.get(pk=review.output_id)
        invocation = AIInvocation.objects.get(pk=review.invocation_id)
        cached = AIInvocation.objects.select_related("output", "inputs_ref").get(
            pk=invocation.pk
        )
        values = AIArtifact.objects.filter(pk=review.output_id).values_list(
            "kind", flat=True
        )
        assert list(values) == ["output"]
        selector = pending_outputs.key(raw)
        assert try_hold_decision(clinic_id=world.setup.clinic_id, using="default")

        def prefetch() -> None:
            prefetch_related_objects([invocation], "output")

        probes: dict[str, Callable[[], object]] = {
            "method-field": lambda: Metadata(invocation).data,
            "representation": lambda: Custom(invocation).data,
            "template": lambda: Template("{{ row.output.kind }}").render(
                Context({"row": invocation})
            ),
            "values": lambda: list(
                AIArtifact.objects.filter(pk=selector.pk).values("kind")
            ),
            "prefetch": prefetch,
            "default-manager": lambda: AIArtifact._default_manager.get(pk=selector.pk),
            "base-manager": lambda: AIArtifact._base_manager.get(pk=selector.pk),
            "raw-attribute": lambda: raw.kind,
            "instance-dictionary": lambda: vars(raw)["kind"],
            "cached-forward": lambda: cached.inputs_ref,
            "cached-reverse": lambda: cached.output,
            "values-list": lambda: list(values.all()),
            "joined-column": lambda: list(
                AIInvocation.objects.filter(pk=invocation.pk).values("output__kind")
            ),
            "cached-values": lambda: list(values),
        }
        unwrapped = probes[route]

        def render(batch: pending_outputs.RenderSelection) -> bool:
            fresh = batch.get(selector)
            assert fresh is not None
            assert fresh.record.kind == "output"
            # A genuine frame does not grant access to different instances or
            # to a second queryset, even one selecting the identical key.
            with pytest.raises(
                RuntimeError, match="pending output requires typed visibility"
            ) as exc:
                unwrapped()
            assert type(exc.value).__name__ == "PendingReadOutsideVisibilityError"
            return True

        with pytest.raises(
            RuntimeError, match="pending output requires typed visibility"
        ) as exc:
            unwrapped()
        assert type(exc.value).__name__ == "PendingReadOutsideVisibilityError"
        assert pending_outputs.ai_pending_visible(
            (selector,), render, clinic_id=world.setup.clinic_id
        )


def test_only_the_issuing_frame_can_read_its_materialized_instance(
    world: RevenueWorld,
) -> None:
    review = request_and_collect(world)
    assert review.output_id is not None
    selector = pending_outputs.PendingKey(AIArtifact, review.output_id)

    def retain(batch: pending_outputs.RenderSelection) -> AIArtifact:
        checked = batch.get(selector)
        assert checked is not None
        assert checked.record.kind == "output"
        return checked.record

    with finance_scope(world.setup):
        row = pending_outputs.ai_pending_visible(
            (selector,), retain, clinic_id=world.setup.clinic_id
        )
        with pytest.raises(
            RuntimeError, match="pending output requires typed visibility"
        ) as exc:
            _ = row.kind
        assert type(exc.value).__name__ == "PendingReadOutsideVisibilityError"
    # Ending the transaction (and therefore the lock) cannot revive a record
    # explicitly materialized by a now-closed frame.
    with pytest.raises(RuntimeError, match="pending output requires typed visibility"):
        _ = row.kind
