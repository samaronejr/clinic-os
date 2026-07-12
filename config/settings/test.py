SECRET_KEY: str = "placeholder-not-secret"
DEBUG = True
ALLOWED_HOSTS: list[str] = ["*"]
INSTALLED_APPS: list[str] = []
MIDDLEWARE: list[str] = []
ROOT_URLCONF = "config.urls"
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    },
}
USE_TZ = True
TIME_ZONE = "UTC"
DEFAULT_AUTO_FIELD = "django.db.models.AutoField"
