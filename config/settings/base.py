import os
from pathlib import Path

import environ

from config.runtime import enforce_wheel_timezone

from .contracts import require_synthetic_mode
from .telemetry import configure_sentry

enforce_wheel_timezone()

BASE_DIR = Path(__file__).resolve().parents[2]
DEFAULT_APP_DATABASE_URL = (
    "postgresql://clinic_app:clinic_app_password@localhost:5432/clinic"
)

env = environ.Env()


def export_settings(namespace: dict[str, object]) -> None:
    namespace.update(
        (name, value) for name, value in globals().items() if name.isupper()
    )


SECRET_KEY: str = env("SECRET_KEY", default="development-only-secret-key")
SECRET_KEY_CONFIGURED: bool = "SECRET_KEY" in os.environ
DEBUG: bool = env.bool("DEBUG", default=False)
ALLOWED_HOSTS: list[str] = env.list("ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])
CLINIC_DATA_MODE: str = require_synthetic_mode(
    env("CLINIC_DATA_MODE", default="synthetic")
)

INSTALLED_APPS: list[str] = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "django_otp",
    "django_otp.plugins.otp_static",
    "django_otp.plugins.otp_totp",
    "apps.identity.apps.IdentityConfig",
    "apps.tenancy.apps.TenancyConfig",
    "apps.scheduling.apps.SchedulingConfig",
    "apps.intake.apps.IntakeConfig",
    "apps.ehr.apps.EhrConfig",
    "apps.teleconsult.apps.TeleconsultConfig",
    "apps.prescription.apps.PrescriptionConfig",
    "apps.consent.apps.ConsentConfig",
    "apps.audit.apps.AuditConfig",
    "apps.billing.apps.BillingConfig",
    "apps.comms.apps.CommsConfig",
    "apps.retention.apps.RetentionConfig",
    "apps.interop.apps.InteropConfig",
]

AUTH_USER_MODEL: str = "identity.User"
AUTHENTICATION_BACKENDS: list[str] = [
    "apps.identity.auth_backends.ClinicBackend",
]
LOGIN_URL: str = "/auth/login/"
OTP_LOGIN_URL: str = "/auth/verify/"
OTP_TOTP_ISSUER: str = "Clinic OS"
STEP_UP_MAX_AGE_SECONDS: int = 300

MIDDLEWARE: list[str] = [
    "apps.core.middleware.ResponsePrivacyMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django_otp.middleware.OTPMiddleware",
    "apps.tenancy.middleware.TenantMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF: str = "config.urls"
WSGI_APPLICATION: str = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

DATABASES = {"default": env.db("APP_DATABASE_URL", default=DEFAULT_APP_DATABASE_URL)}
DATABASES["default"]["ATOMIC_REQUESTS"] = False
DATABASES["default"]["OPTIONS"] = {"options": "-c search_path=clinic_app,public"}

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": "60/min",
        "user": "600/min",
    },
}

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": (
            "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"
        )
    },
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE: str = "en-us"
TIME_ZONE: str = "UTC"
USE_I18N: bool = True
USE_TZ: bool = True
STATIC_URL: str = "/static/"
STATIC_ROOT: Path = BASE_DIR / "staticfiles"
STATICFILES_DIRS: list[Path] = [BASE_DIR / "static"]
PRODUCTION_STATICFILES_BACKEND: str = (
    "whitenoise.storage.CompressedManifestStaticFilesStorage"
)
_settings_module = os.environ.get("DJANGO_SETTINGS_MODULE", "")
_direct_static_modules = {
    "config.settings.browser",
    "config.settings.dev",
    "config.settings.test",
}
STATICFILES_BACKEND: str = (
    "django.contrib.staticfiles.storage.StaticFilesStorage"
    if _settings_module in _direct_static_modules
    else PRODUCTION_STATICFILES_BACKEND
)
STORAGES: dict[str, dict[str, str]] = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": STATICFILES_BACKEND},
}
WHITENOISE_AUTOREFRESH: bool = _settings_module == "config.settings.test"
DEFAULT_AUTO_FIELD: str = "django.db.models.BigAutoField"

SECURE_CONTENT_TYPE_NOSNIFF: bool = True
SECURE_REFERRER_POLICY: str = "same-origin"
X_FRAME_OPTIONS: str = "DENY"

SENTRY_DSN: str = env("SENTRY_DSN", default="")
configure_sentry(SENTRY_DSN)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "structured": {
            "format": "{asctime} {levelname} {name} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "structured",
        },
    },
    "root": {"handlers": ["console"], "level": "INFO"},
}
