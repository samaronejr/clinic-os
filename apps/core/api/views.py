"""RPC-style POST views of the internal UI API.

Each view validates its JSON body, calls one keyword-only service with the
explicit clinic scope from the body, and serializes the returned frozen
dataclass. Authority still comes from the request tenant transaction and the
service's own role checks; refusals use the ``{code, message_key}`` contract.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

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
)
from apps.core.api.serializers import (
    AgendaPageSerializer,
    AgendaQueryRequestSerializer,
    ErrorSerializer,
)
from apps.scheduling.services import (
    AgendaInputError,
    AvailabilityAccessDeniedError,
    view_agenda,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from rest_framework.request import Request

    type ExceptionHandler = Callable[[Exception, Mapping[str, object]], Response | None]

ERROR_RESPONSES: dict[int, type[ErrorSerializer]] = {
    400: ErrorSerializer,
    403: ErrorSerializer,
    405: ErrorSerializer,
    406: ErrorSerializer,
    415: ErrorSerializer,
    429: ErrorSerializer,
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
