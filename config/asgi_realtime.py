"""Isolated SSE ASGI entrypoint, never the ordinary HTTP application."""

import os

from django.core.asgi import get_asgi_application

os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings.realtime"
application = get_asgi_application()
