"""Mount empty querysets and run the real structural gate. No request or row."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

import pytest
from apps.ai.models import AIInvocation, CareProposal
from apps.ai.pending_outputs import VisiblePending
from apps.identity.models import Organization
from apps.workflows.models import CareAction
from django.test import override_settings
from django.urls import include, path, resolve
from rest_framework import generics, serializers, viewsets
from rest_framework.routers import SimpleRouter
from rest_framework.views import APIView

from ai import test_pending_output_structure as structure

if TYPE_CHECKING:
    from collections.abc import Callable

    from django.http import HttpResponseBase


class AllFields(serializers.ModelSerializer[CareProposal]):
    class Meta:
        model = CareProposal
        fields = "__all__"


class ExceptPosition(serializers.ModelSerializer[CareProposal]):
    class Meta:
        model = CareProposal
        exclude = ("position",)


class State(serializers.Serializer[Any]):
    state = serializers.ReadOnlyField()


class Output(serializers.Serializer[Any]):
    kind = serializers.ReadOnlyField()


class Envelope(serializers.Serializer[Any]):
    output = Output()


class Collection(serializers.Serializer[Any]):
    proposals = State(many=True, source="careproposal_set")


class List(generics.ListAPIView[Any]):
    queryset = CareProposal.objects.none()
    serializer_class = State


class RoutedSet(viewsets.ReadOnlyModelViewSet[Any]):
    queryset = AIInvocation.objects.none()
    serializer_class = State

    def get_serializer_class(self) -> type[serializers.BaseSerializer[Any]]:
        return Envelope if self.action == "retrieve" else State


def _guard(callback: Callable[..., HttpResponseBase]) -> None:
    routes = (path("probe/", include([path("preview/", callback)])),)
    with override_settings(ROOT_URLCONF=routes):
        structure.test_pending_models_have_only_one_data_access_boundary()


@pytest.mark.parametrize(
    ("callback", "binding"),
    [
        (List.as_view(serializer_class=AllFields), "CareProposal.state"),
        (List.as_view(serializer_class=ExceptPosition), "CareProposal.state"),
        (List.as_view(), "CareProposal.state"),
        (RoutedSet.as_view({"get": "retrieve"}), "output.kind -> AIArtifact.kind"),
        (
            List.as_view(
                queryset=Organization.objects.none(), serializer_class=Collection
            ),
            "proposals.state -> CareProposal.state",
        ),
    ],
    ids=("all", "exclude", "implicit", "nested-action", "nested-list"),
)
def test_resolved_raw_fields_fail_the_project_guard(
    callback: Callable[..., HttpResponseBase], binding: str
) -> None:
    with pytest.raises(AssertionError, match="probe/preview/") as failure:
        _guard(callback)
    assert binding in str(failure.value)


def test_implicit_field_on_an_ordinary_model_is_not_pending() -> None:
    _guard(List.as_view(queryset=CareAction.objects.none()))


def test_checked_projection_dto_is_not_a_raw_model_binding() -> None:
    class Checked(serializers.Serializer[VisiblePending[CareProposal]]):
        state = serializers.ReadOnlyField(source="record.state")

    class Preview(APIView):
        serializer_class = Checked

        def get(self) -> None:
            pytest.fail("route must not be dispatched")

    _guard(Preview.as_view())


def test_missing_serializer_fails_closed_with_the_route_and_reason() -> None:
    class Unknown(APIView):
        def get(self) -> None:
            pytest.fail("route must not be dispatched")

    with pytest.raises(
        AssertionError, match=r"probe/preview/ Unknown|probe/preview/ .*Unknown"
    ) as failure:
        _guard(Unknown.as_view())
    assert "unresolved serializer" in str(failure.value)
    assert "no output serializer class" in str(failure.value)


def test_unresolvable_action_fails_closed_despite_declared_fallback() -> None:
    class Dynamic(List):
        def get_serializer_class(self) -> type[serializers.BaseSerializer[Any]]:
            return State if self.request.user.is_staff else AllFields

    with pytest.raises(AssertionError, match=r"probe/preview/.*Dynamic.*unresolved"):
        _guard(Dynamic.as_view(queryset=CareAction.objects.none()))


def test_implicitly_bound_model_property_is_guarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def summary(row: CareProposal) -> str:
        pytest.fail("guard evaluated a model property")

    monkeypatch.setattr(CareProposal, "summary", property(summary), raising=False)

    class Identity(serializers.ModelSerializer[CareProposal]):
        class Meta:
            model = CareProposal
            fields: ClassVar = ("summary",)

    with pytest.raises(AssertionError, match=r"summary -> CareProposal\.summary"):
        _guard(List.as_view(serializer_class=Identity))


def test_unmounted_generic_binding_cannot_wait_for_url_registration() -> None:
    from ai.pending_boundary_inventory import inventory, violations  # noqa: PLC0415
    from ai.pending_declarations import DECLARATIVE  # noqa: PLC0415

    source = """
from django.db.models import QuerySet
from rest_framework import generics, serializers
from apps.ai.models import CareProposal
from apps.workflows.models import CareAction
class Status(serializers.Serializer):
    state = serializers.ReadOnlyField()
class Preview(generics.ListAPIView):
    serializer_class = Status
    queryset = QuerySet(model=CareAction).none()
"""
    assert not violations(source, inventory())
    mutant = source.replace("model=CareAction", "model=CareProposal")
    assert DECLARATIVE in {reason for _, reason in violations(mutant, inventory())}


def test_secondary_guard_refuses_opaque_method_on_provenance_input() -> None:
    class RelatedMetadata(serializers.Serializer[AIInvocation]):
        category = serializers.SerializerMethodField()

        def get_category(self, obj: AIInvocation) -> str:
            return obj.output.kind

    with pytest.raises(
        AssertionError, match=r"probe/preview/.*opaque method field category"
    ):
        _guard(
            List.as_view(
                queryset=AIInvocation.objects.none(), serializer_class=RelatedMetadata
            )
        )


@pytest.mark.parametrize("by_value", [False, True], ids=["present", "value"])
def test_reachable_detail_selector_cannot_hide_behind_empty_kwargs(
    by_value: bool,
) -> None:
    class Detail(viewsets.ReadOnlyModelViewSet[Any]):
        queryset = CareAction.objects.none()
        serializer_class = State

        def get_serializer_class(self) -> type[serializers.BaseSerializer[Any]]:
            if (
                "pk" in self.kwargs
                and "clinic" in self.kwargs
                and (not by_value or self.kwargs["pk"] == "reviewed")
            ):
                msg = "detail selector cannot resolve"
                raise ValueError(msg)
            return State

    router = SimpleRouter()
    router.register("rows", Detail, basename="rows")
    routes = (path("at/<int:clinic>/", include(router.urls)),)
    with override_settings(ROOT_URLCONF=routes):
        match = resolve("/at/7/rows/reviewed/")
        assert match.kwargs == {"clinic": 7, "pk": "reviewed"}
        with pytest.raises(
            AssertionError, match=r"Detail\.retrieve .*unresolved serializer"
        ) as exc:
            structure.test_pending_models_have_only_one_data_access_boundary()
    assert (
        "URL capture 'pk'" if by_value else "detail selector cannot resolve"
    ) in str(exc.value)
