from __future__ import annotations

from inspect import signature
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

from apps.identity.stepup import assert_step_up, require_recent_verification
from config.urls import urlpatterns as production_urlpatterns
from django.http import HttpRequest, HttpResponse, HttpResponseBase
from django.urls import path

if TYPE_CHECKING:
    from collections.abc import Mapping

    from django.urls.resolvers import URLPattern, URLResolver

type RuntimeMaxAge = float | str | bool | int

INVALID_MAX_AGES: Final[Mapping[str, RuntimeMaxAge]] = MappingProxyType(
    {
        "nan": float("nan"),
        "infinity": float("inf"),
        "finite-float": 300.0,
        "string": "300",
        "bool": True,
        "negative": -1,
    }
)


@require_recent_verification(max_age=300)
def issuance_hook_probe(_request: HttpRequest) -> HttpResponseBase:
    return HttpResponse("issuance hook reached")


def raw_issuance_hook_probe(request: HttpRequest) -> HttpResponseBase:
    assert_step_up(request, max_age=300)
    return HttpResponse("raw issuance hook reached")


def _invalid_limit_action(_request: HttpRequest) -> HttpResponseBase:
    return HttpResponse("invalid limit action executed")


def runtime_limit_raw_probe(request: HttpRequest) -> HttpResponseBase:
    limit = INVALID_MAX_AGES[request.GET["case"]]
    bound = signature(assert_step_up).bind(request, max_age=limit)
    assert_step_up(*bound.args, **bound.kwargs)
    return _invalid_limit_action(request)


def runtime_limit_decorator_probe(request: HttpRequest) -> HttpResponseBase:
    limit = INVALID_MAX_AGES[request.GET["case"]]
    bound = signature(require_recent_verification).bind(max_age=limit)
    decorator = require_recent_verification(*bound.args, **bound.kwargs)
    return decorator(_invalid_limit_action)(request)


urlpatterns: list[URLPattern | URLResolver] = [
    *production_urlpatterns,
    path("__test__/issuance/", issuance_hook_probe, name="issuance-hook-probe"),
    path(
        "__test__/raw-issuance/",
        raw_issuance_hook_probe,
        name="raw-issuance-hook-probe",
    ),
    path(
        "__test__/runtime-limit-raw/",
        runtime_limit_raw_probe,
        name="runtime-limit-raw-probe",
    ),
    path(
        "__test__/runtime-limit-decorator/",
        runtime_limit_decorator_probe,
        name="runtime-limit-decorator-probe",
    ),
]
