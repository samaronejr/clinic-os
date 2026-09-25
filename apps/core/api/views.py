"""RPC-style POST views of the internal UI API.

Each view validates its JSON body, calls one keyword-only service with the
explicit clinic scope from the body, and serializes the returned frozen
dataclass. Authority still comes from the request tenant transaction and the
service's own role checks; refusals use the ``{code, message_key}`` contract.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from django.views.decorators.csrf import csrf_exempt
from drf_spectacular.utils import extend_schema
from rest_framework.parsers import JSONParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.api.authentication import StepUpVerified, UiSessionAuthentication
from apps.core.api.errors import (
    ACCESS_DENIED,
    INVALID_INPUT,
    UiApiError,
    ui_api_exception_handler,
    ui_api_not_found_response,
)
from apps.core.api.serializers import (
    AgendaPageSerializer,
    AgendaQueryRequestSerializer,
    CommandResultSerializer,
    CommandSearchRequestSerializer,
    ErrorSerializer,
)
from apps.core.command_search import search_commands
from apps.core.command_views import tokenized
from apps.core.workspace import ACTIVE_CLINIC_SESSION_KEY, clinic_of_actor
from apps.identity.models import User
from apps.scheduling.services import (
    AgendaInputError,
    AvailabilityAccessDeniedError,
    view_agenda,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from django.http import HttpRequest, JsonResponse
    from rest_framework.request import Request

    type ExceptionHandler = Callable[[Exception, Mapping[str, object]], Response | None]

ERROR_RESPONSES: dict[int, type[ErrorSerializer]] = {
    400: ErrorSerializer,
    403: ErrorSerializer,
    405: ErrorSerializer,
    406: ErrorSerializer,
    415: ErrorSerializer,
    429: ErrorSerializer,
    500: ErrorSerializer,
}


class UiApiView(APIView):
    """Base for every ``/api/ui/v1/`` endpoint: POST-only JSON over session."""

    authentication_classes = (UiSessionAuthentication,)
    permission_classes = (IsAuthenticated, StepUpVerified)
    parser_classes = (JSONParser,)
    renderer_classes = (JSONRenderer,)
    http_method_names = ["post"]  # noqa: RUF012 - Django View instance attribute

    def get_exception_handler(self) -> ExceptionHandler:
        """Route every refusal through the ``{code, message_key}`` contract."""
        return ui_api_exception_handler


class AgendaQueryView(UiApiView):
    """Return one authorized clinic-local day or ISO-week agenda page."""

    @extend_schema(
        operation_id="agenda_query",
        tags=["agenda"],
        request=AgendaQueryRequestSerializer,
        responses={200: AgendaPageSerializer, **ERROR_RESPONSES},
    )
    def post(self, request: Request) -> Response:
        """Adapt ``scheduling.view_agenda``; unknown and foreign clinics match."""
        query = AgendaQueryRequestSerializer(data=request.data)
        query.is_valid(raise_exception=True)
        data = query.validated_data
        try:
            page = view_agenda(
                clinic_id=data["clinic_id"],
                view=data["view"],
                date=data["date"].isoformat(),
                page=data["page"],
            )
        except AvailabilityAccessDeniedError as error:
            raise UiApiError(ACCESS_DENIED) from error
        except AgendaInputError as error:
            raise UiApiError(INVALID_INPUT) from error
        return Response(AgendaPageSerializer(page).data)


class CommandSearchView(UiApiView):
    """Return the actor's allowed palette rows for one query in one clinic."""

    @extend_schema(
        operation_id="command_search",
        tags=["command"],
        request=CommandSearchRequestSerializer,
        responses={200: CommandResultSerializer(many=True), **ERROR_RESPONSES},
    )
    def post(self, request: Request) -> Response:
        """Adapt ``core.search_commands``; unknown and foreign clinics answer ``[]``."""
        query = CommandSearchRequestSerializer(data=request.data)
        query.is_valid(raise_exception=True)
        data = query.validated_data
        user = request.user
        if not isinstance(user, User):
            raise UiApiError(ACCESS_DENIED)
        clinic_id = data.get("clinic_id") or _remembered_clinic(request)
        clinic = clinic_of_actor(user.pk, clinic_id) if clinic_id else None
        if clinic is None:
            return Response([])
        search = search_commands(
            clinic_id=clinic.id,
            timezone=clinic.timezone,
            roles=clinic.roles,
            query=data["q"],
            context_path=data.get("page_path", ""),
        )
        rows = [
            {
                "kind": result.kind,
                "label": result.label,
                "meta": result.meta,
                "action_url_name": result.action_url_name,
                "token": token or None,
                "href": result.href,
            }
            for result, token in tokenized(request.session, clinic.id, search)
        ]
        return Response(CommandResultSerializer(rows, many=True).data)


def _remembered_clinic(request: Request) -> UUID | None:
    raw = request.session.get(ACTIVE_CLINIC_SESSION_KEY)
    try:
        return UUID(raw) if isinstance(raw, str) else None
    except ValueError:
        return None


@csrf_exempt
def not_found(request: HttpRequest) -> JsonResponse:  # noqa: ARG001 - Django view
    """Answer any unpublished ``/api/ui/v1/`` route with the JSON 404 body.

    CSRF-exempt because it reads nothing and changes nothing; a CSRF refusal
    here would only swap the contract body for Django's HTML page.
    """
    return ui_api_not_found_response()
