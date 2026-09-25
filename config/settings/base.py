import os
from pathlib import Path

import environ

from config.runtime import enforce_wheel_timezone

from .contracts import (
    require_data_mode,
    resolve_attachment_root,
    resolve_csp_report_only,
    validate_secret_store_env,
)
from .database import DEFAULT_APP_DATABASE_URL
from .telemetry import configure_sentry

enforce_wheel_timezone()

BASE_DIR = Path(__file__).resolve().parents[2]

env = environ.Env()


def export_settings(namespace: dict[str, object]) -> None:
    namespace.update(
        (name, value) for name, value in globals().items() if name.isupper()
    )


SECRET_KEY: str = env("SECRET_KEY", default="development-only-secret-key")
SECRET_KEY_CONFIGURED: bool = "SECRET_KEY" in os.environ
DEBUG: bool = env.bool("DEBUG", default=False)
ALLOWED_HOSTS: list[str] = env.list("ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])
# Synthetic is the default; 'live' starts only with a valid, separately
# authorized activation record bound to this release, environment, evidence
# and storage (ops.release.activation). Anything else fails closed.
CLINIC_DATA_MODE: str = require_data_mode(env("CLINIC_DATA_MODE", default="synthetic"))
# Strict CSP is enforced by default; report-only is a synthetic-only rollout
# and rollback lever that live mode refuses (contracts.resolve_csp_report_only).
CLINIC_CSP_REPORT_ONLY: bool = resolve_csp_report_only(os.environ, CLINIC_DATA_MODE)
# Managed-secret boundary for key material; no default, no plaintext fallback.
CLINIC_SECRET_BACKEND: str | None
CLINIC_SECRET_DIR: str | None
CLINIC_SECRET_BACKEND, CLINIC_SECRET_DIR = validate_secret_store_env(
    env("CLINIC_SECRET_BACKEND", default=None),
    env("CLINIC_SECRET_DIR", default=None),
)

INSTALLED_APPS: list[str] = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "drf_spectacular",
    "django_otp",
    "django_otp.plugins.otp_static",
    "django_otp.plugins.otp_totp",
    "apps.core.apps.CoreConfig",
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
    "apps.providers.apps.ProvidersConfig",
    "apps.realtime.apps.RealtimeConfig",
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
    # Halts every product request once the live activation is disabled or
    # drifted; a pass-through outside live mode.
    "apps.core.middleware.LiveModeHaltMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    # Strict first-party Content-Security-Policy on every Django response,
    # refusals included; static files served above it need none.
    "apps.core.middleware.ContentSecurityPolicyMiddleware",
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
if "OPTIONS" not in DATABASES["default"]:
    DATABASES["default"]["OPTIONS"] = {}
DATABASES["default"]["OPTIONS"]["options"] = "-c search_path=clinic_app,public"
DATABASES["default"]["OPTIONS"]["prepare_threshold"] = None
DATABASES["default"]["DISABLE_SERVER_SIDE_CURSORS"] = True
DATABASES["locks"] = env.db_url_config(
    env.str(
        "LOCKS_DATABASE_URL",
        default=env.str("APP_DATABASE_URL", default=DEFAULT_APP_DATABASE_URL),
    )
)
DATABASES["locks"]["ATOMIC_REQUESTS"] = False
DATABASES["locks"].setdefault("OPTIONS", {}).update(
    options="-c search_path=clinic_app,public", prepare_threshold=None
)

# Optional hints, never the authority for a read/write. Disable to retain polling.
REALTIME_ENABLED: bool = env.bool("REALTIME_ENABLED", default=False)
REALTIME_POLLING_FALLBACK: bool = env.bool("REALTIME_POLLING_FALLBACK", default=True)
REALTIME_REDIS_URL: str = env.str(
    "REALTIME_REDIS_URL", default="redis://localhost:6379/1"
)
REALTIME_TOPIC_SECRET: str = env.str("REALTIME_TOPIC_SECRET", default=SECRET_KEY)

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
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}

# The committed docs/api/ui-v1.yaml is the internal UI API contract;
# tests/infra/test_openapi_drift.py regenerates it and fails on any diff.
SPECTACULAR_SETTINGS: dict[str, object] = {
    "TITLE": "Clinic Ops internal UI API",
    "DESCRIPTION": (
        "RPC-style POST endpoints for first-party UI surfaces. Record "
        "identifiers travel only in request bodies. Session authentication "
        "with the X-CSRFToken header; errors are {code, message_key}."
    ),
    "VERSION": "1",
    "SERVE_INCLUDE_SCHEMA": False,
    "SCHEMA_PATH_PREFIX": r"/api/ui/v1",
    "PREPROCESSING_HOOKS": ["apps.core.api.schema.ui_api_endpoints_only"],
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

LANGUAGE_CODE: str = "pt-br"
LANGUAGES: list[tuple[str, str]] = [("pt-br", "Português (Brasil)")]
LOCALE_PATHS: list[Path] = [BASE_DIR / "locale"]
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
WHITENOISE_MIMETYPES: dict[str, str] = {".webmanifest": "application/manifest+json"}
DEFAULT_AUTO_FIELD: str = "django.db.models.BigAutoField"

SECURE_CONTENT_TYPE_NOSNIFF: bool = True
SECURE_REFERRER_POLICY: str = "same-origin"
X_FRAME_OPTIONS: str = "DENY"

SENTRY_DSN: str = env("SENTRY_DSN", default="")
configure_sentry(SENTRY_DSN)

# Synthetic attachment object store; production needs an approved backend.
# Resolved through the shared contract seam so the startup isolation check
# evaluates the identical effective root (including $VARIABLE proxies).
EHR_ATTACHMENT_ROOT: Path = Path(resolve_attachment_root(os.environ))

CELERY_BROKER_URL: str = env(
    "CELERY_BROKER_URL",
    default="redis://localhost:6379/0",
)
CELERY_RESULT_BACKEND: str | None = env("CELERY_RESULT_BACKEND", default=None)
# Synthetic-only, independently opted in; never authorizes a real provider.
COMMS_SYNTHETIC_CHANNELS: list[str] = env.list("COMMS_SYNTHETIC_CHANNELS", default=[])
# Synthetic-only fault injection for integration gates; empty means no faults.
COMMS_SYNTHETIC_FAILURE_CHANNELS: list[str] = env.list(
    "COMMS_SYNTHETIC_FAILURE_CHANNELS",
    default=[],
)
# Synthetic-only video room gate; the task-6 video capability stays unavailable.
TELECONSULT_SYNTHETIC_PROVIDER: bool = env.bool(
    "TELECONSULT_SYNTHETIC_PROVIDER",
    default=False,
)
# Synthetic-only signing/registry rehearsal gates; no real capability exists.
PRESCRIPTION_SYNTHETIC_SIGNING: bool = env.bool(
    "PRESCRIPTION_SYNTHETIC_SIGNING",
    default=False,
)
PHYSICIAN_SYNTHETIC_REGISTRY: bool = env.bool(
    "PHYSICIAN_SYNTHETIC_REGISTRY",
    default=False,
)
TELECONSULT_SYNTHETIC_FAIL: bool = env.bool(
    "TELECONSULT_SYNTHETIC_FAIL",
    default=False,
)
# Synthetic-only PIX rehearsal gate; no provider is approved or reachable, and
# the generated codes are explicitly non-payable.
BILLING_SYNTHETIC_PIX: bool = env.bool("BILLING_SYNTHETIC_PIX", default=False)
BILLING_SYNTHETIC_PIX_SECRET: str = env("BILLING_SYNTHETIC_PIX_SECRET", default="")
COMMS_REVOKED_REMINDER_TEMPLATES: list[str] = env.list(
    "COMMS_REVOKED_REMINDER_TEMPLATES", default=[]
)
# Eager execution is a test-only override; production dispatch is brokered.
CELERY_TASK_ALWAYS_EAGER: bool = env.bool(
    "CELERY_TASK_ALWAYS_EAGER",
    default=False,
)

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
