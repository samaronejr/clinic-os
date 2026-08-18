from __future__ import annotations

import tomllib
from base64 import b64encode
from hashlib import sha384
from pathlib import Path

from config.settings import base
from django.conf import settings
from django.contrib.staticfiles import finders

PROJECT_ROOT = Path(__file__).resolve().parents[1]
HTMX_SHA384 = "H5SrcfygHmAuTDZphMHqBJLc3FhssKjG7w/CeCpFReSfwBWDTKpkzPP8c+cLsK+V"


def test_todo18_dependencies_are_exact_and_isolated() -> None:
    document = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())

    assert document["project"]["dependencies"][-2:] == [
        "tzdata==2026.3",
        "whitenoise==6.12.0",
    ]
    assert document["dependency-groups"]["browser"] == [
        "playwright==1.61.0",
        "pytest-playwright==0.8.0",
    ]
    serialized = (PROJECT_ROOT / "pyproject.toml").read_text()
    assert "jsonschema" not in serialized
    assert "basedpyright" not in serialized


def test_production_static_is_manifested_and_self_hosted() -> None:
    assert settings.MIDDLEWARE[:2] == [
        "django.middleware.security.SecurityMiddleware",
        "whitenoise.middleware.WhiteNoiseMiddleware",
    ]
    assert base.PRODUCTION_STATICFILES_BACKEND == (
        "whitenoise.storage.CompressedManifestStaticFilesStorage"
    )
    assert settings.STORAGES["staticfiles"]["BACKEND"] == (
        "django.contrib.staticfiles.storage.StaticFilesStorage"
    )
    assert settings.STATIC_ROOT == PROJECT_ROOT / "staticfiles"

    template = (PROJECT_ROOT / "templates/base.html").read_text()
    assert "{% static 'vendor/htmx/htmx.min.js' %}" in template
    assert "https://" not in template
    assert "http://" not in template

    asset = finders.find("vendor/htmx/htmx.min.js")
    license_path = finders.find("vendor/htmx/LICENSE")
    assert isinstance(asset, str)
    assert isinstance(license_path, str)
    digest = b64encode(sha384(Path(asset).read_bytes()).digest()).decode("ascii")
    assert digest == HTMX_SHA384
    assert "Zero-Clause BSD" in Path(license_path).read_text(encoding="utf-8")
