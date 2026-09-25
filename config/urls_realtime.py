from apps.realtime.views import stream_view
from django.urls import path

urlpatterns = [path("rt/stream", stream_view, name="realtime-stream")]
