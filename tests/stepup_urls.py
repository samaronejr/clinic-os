from apps.identity.stepup import assert_step_up, require_recent_verification
from config.urls import urlpatterns as production_urlpatterns
from django.http import HttpRequest, HttpResponse, HttpResponseBase
from django.urls import path
from django.urls.resolvers import URLPattern, URLResolver


@require_recent_verification(max_age=300)
def issuance_hook_probe(_request: HttpRequest) -> HttpResponseBase:
    return HttpResponse("issuance hook reached")


def raw_issuance_hook_probe(request: HttpRequest) -> HttpResponseBase:
    assert_step_up(request, max_age=300)
    return HttpResponse("raw issuance hook reached")


urlpatterns: list[URLPattern | URLResolver] = [
    *production_urlpatterns,
    path("__test__/issuance/", issuance_hook_probe, name="issuance-hook-probe"),
    path(
        "__test__/raw-issuance/",
        raw_issuance_hook_probe,
        name="raw-issuance-hook-probe",
    ),
]
