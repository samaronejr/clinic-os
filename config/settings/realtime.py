"""SSE-only process; ordinary HTTP traffic must remain on WSGI."""

import os
from importlib import import_module

from django.core.exceptions import ImproperlyConfigured

# Production is fail-closed by default. Local/renewal are explicit synthetic
# process modes, not relaxations available to a live-data deployment.
_mode = os.environ.get("CLINIC_REALTIME_MODE", "production")
_modules = {
    "production": "config.settings.prod",
    "local": "config.settings.base",
    "renewal": "config.settings.renewal",
}
if _mode not in _modules:
    message = "invalid realtime process mode"
    raise ImproperlyConfigured(message)
_parent = import_module(_modules[_mode])
globals().update((key, value) for key, value in vars(_parent).items() if key.isupper())
if _mode != "production" and _parent.CLINIC_DATA_MODE != "synthetic":
    message = "local realtime requires synthetic data mode"
    raise ImproperlyConfigured(message)

ROOT_URLCONF = "config.urls_realtime"
ASGI_APPLICATION = "config.asgi_realtime.application"
globals().pop("WSGI_APPLICATION", None)
MIDDLEWARE = [
    "apps.core.middleware.ResponsePrivacyMiddleware",
    "apps.core.middleware.LiveModeHaltMiddleware",
    "apps.core.middleware.ContentSecurityPolicyMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django_otp.middleware.OTPMiddleware",
]
DATABASES = _parent.DATABASES
for _database in DATABASES.values():
    _database["CONN_MAX_AGE"] = 0
    _database["ATOMIC_REQUESTS"] = False
    _database.setdefault("OPTIONS", {})["prepare_threshold"] = None
