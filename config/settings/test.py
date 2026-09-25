import environ

from .base import *  # noqa: F403

test_env = environ.Env()
DATABASES = {
    "default": test_env.db(
        "MIGRATION_DATABASE_URL",
        default="postgresql://clinic_owner:clinic_owner_password@localhost:5432/clinic",
    )
}
DATABASES["default"]["ATOMIC_REQUESTS"] = False
DATABASES["default"].setdefault("OPTIONS", {})["options"] = (
    "-c search_path=clinic_app,public"
)
PASSWORD_HASHERS: list[str] = ["django.contrib.auth.hashers.MD5PasswordHasher"]
# Test runs never reach for a developer workstation's Redis: base defaults
# the broker to redis://localhost:6379/0, which is often another project's
# broker. An explicit CELERY_BROKER_URL still wins (the real-broker gate
# CLINIC_BROKER_GATE=required sets its own URL); the result backend keeps
# the disabled default.
CELERY_BROKER_URL = test_env("CELERY_BROKER_URL", default="memory://")
CELERY_RESULT_BACKEND = test_env("CELERY_RESULT_BACKEND", default=None)
