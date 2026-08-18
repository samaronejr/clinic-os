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
