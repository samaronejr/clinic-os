"""Body-scoped commercial API routing."""

from django.urls import path

from apps.subscriptions.views import commercial

app_name = "subscriptions"
urlpatterns = [path("api/subscriptions/v1/", commercial, name="commercial")]
