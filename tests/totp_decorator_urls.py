from __future__ import annotations

from typing import TYPE_CHECKING, Final

from apps.identity.otp import auth_response, privileged_totp_required
from django.http import HttpRequest, HttpResponse, HttpResponseBase
from django.urls import include, path

if TYPE_CHECKING:
    from django.urls.resolvers import URLPattern, URLResolver

BODY_SENTINEL: Final = "todo14-decorator-body-sentinel"
QUERY_SENTINEL: Final = "todo14-decorator-query-sentinel"


def block_continuation(clinic_id: str, block_id: str) -> str:
    return f"/decorator/{clinic_id}/{block_id}/"


@privileged_totp_required(block_continuation)
def block_view(
    request: HttpRequest,
    clinic_id: str,
    block_id: str,
) -> HttpResponseBase:
    return auth_response(
        HttpResponse(f"{request.method}|{clinic_id}|{block_id}", status=200)
    )


def keyword_continuation(*, clinic_id: str) -> str:
    return f"/decorator-keyword/{clinic_id}/"


@privileged_totp_required(keyword_continuation)
def keyword_view(request: HttpRequest, *, clinic_id: str) -> HttpResponseBase:
    return auth_response(HttpResponse(f"{request.method}|{clinic_id}", status=200))


urlpatterns: list[URLPattern | URLResolver] = [
    path(
        "decorator/<str:clinic_id>/<str:block_id>/",
        block_view,
        name="decorator-block",
    ),
    path(
        "decorator-keyword/<str:clinic_id>/",
        keyword_view,
        name="decorator-keyword",
    ),
    path("", include("apps.identity.urls")),
]
