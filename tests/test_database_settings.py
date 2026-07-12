from django.conf import settings


def test_test_settings_use_the_migration_owner_database() -> None:
    # Given: pytest has loaded the Todo 2 test settings module
    # When: the default database configuration is inspected
    # Then: tests connect with the migration owner rather than SQLite
    database = settings.DATABASES["default"]

    assert database["ENGINE"] == "django.db.backends.postgresql"
    assert database["USER"] == "clinic_owner"


def test_settings_keep_runtime_database_requests_non_atomic() -> None:
    # Given: the Todo 2 test database configuration
    # When: its transactional request behavior is inspected
    # Then: requests are not implicitly wrapped in database transactions
    database = settings.DATABASES["default"]

    assert database["ATOMIC_REQUESTS"] is False


def test_settings_register_only_foundation_apps() -> None:
    # Given: the Todo 2 Django application registry
    # When: installed applications are inspected
    # Then: no domain app is registered before Todo 4
    installed_apps = settings.INSTALLED_APPS

    assert "rest_framework" in installed_apps
    assert "django_otp" in installed_apps
    assert "django_otp.plugins.otp_static" in installed_apps
    assert "django_otp.plugins.otp_totp" in installed_apps
    assert not any(app_name.startswith("apps.") for app_name in installed_apps)
