"""Body-only CRM selectors and a fixed public booking URL."""

from django.urls import path

from apps.crm import views

app_name = "crm"
urlpatterns = [
    path("book/", views.public_booking, name="public-booking"),
    path("book/access/", views.public_access, name="public-access"),
    path("clinics/<uuid:clinic_id>/crm/", views.manager_page, name="manager"),
    path("patient/outreach/", views.preferences, name="preferences"),
    path("patient/feedback/", views.satisfaction, name="satisfaction"),
]
