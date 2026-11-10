"""Public, GET-only website routes; every path is listed in the tenancy bypass."""

from django.urls import path
from django.urls.resolvers import URLPattern

from apps.website import views

app_name = "website"
urlpatterns: list[URLPattern] = [
    path("product/", views.product, name="product"),
    path("security/", views.security, name="security"),
    path("contact/", views.contact, name="contact"),
    path("brand/", views.brand, name="brand"),
    path("pricing/", views.pricing, name="pricing"),
    path("help/", views.help_index, name="help"),
    path("help/<slug:slug>/", views.help_guide, name="guide"),
]
