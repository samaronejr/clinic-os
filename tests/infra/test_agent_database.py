"""Fail-closed machine DSN resolution and dedicated optional alias."""

from __future__ import annotations

import traceback
from typing import TYPE_CHECKING

import pytest
from config.settings.database import agent_database_config
from django.core.exceptions import ImproperlyConfigured

if TYPE_CHECKING:
    from pathlib import Path

PRIMARY = {
    "ENGINE": "django.db.backends.postgresql",
    "HOST": "127.0.0.1",
    "PORT": "5433",
    "NAME": "synthetic",
}


def test_absent_agent_url_never_reuses_staff_credentials() -> None:
    assert agent_database_config({}, primary=PRIMARY) == {}


def test_agent_alias_uses_separate_login_with_exact_options() -> None:
    environment = {
        "AGENT_DATABASE_URL": "$SYNTHETIC_AGENT_DSN",
        "SYNTHETIC_AGENT_DSN": "postgresql://clinic_agent:synthetic@127.0.0.1:5433/synthetic",
    }
    alias = agent_database_config(environment, primary=PRIMARY)
    assert set(alias) == {"agent"}
    assert alias["agent"]["USER"] == "clinic_agent"
    assert alias["agent"]["ATOMIC_REQUESTS"] is False
    assert alias["agent"]["OPTIONS"] == {"options": "-c search_path=clinic_app,public"}


@pytest.mark.parametrize(
    "value",
    [
        "",
        "not-a-dsn",
        "postgresql://clinic_owner:synthetic@127.0.0.1:5433/synthetic",
        "postgresql://clinic_agent@127.0.0.1:5433/synthetic",
        "postgresql://clinic_agent:synthetic@127.0.0.1:5434/synthetic",
        "postgresql://clinic_agent:synthetic@127.0.0.1:5433/other",
        "postgresql://clinic_agent:synthetic@other.invalid:5433/synthetic",
        "postgresql://clinic_agent:synthetic@127.0.0.1:5433/synthetic?options=-c%20role=clinic_owner",
    ],
)
def test_invalid_agent_dsn_fails_without_reflecting_credentials(value: str) -> None:
    with pytest.raises(ImproperlyConfigured) as error:
        agent_database_config({"AGENT_DATABASE_URL": value}, primary=PRIMARY)
    assert (
        str(error.value)
        == "database settings violate the fail-closed database contract"
    )


def test_malformed_agent_url_cannot_leak_a_credential_in_the_traceback() -> None:
    sentinel = "SINTETICO-SENTINELA-DB-CREDENTIAL"
    # urlsplit's NFKC-netloc error contains the entire untrusted authority.
    dsn = (
        f"postgresql://clinic_agent:{sentinel}@"
        "bad\N{FULLWIDTH SOLIDUS}host:5433/synthetic"
    )
    with pytest.raises(ImproperlyConfigured) as error:
        agent_database_config({"AGENT_DATABASE_URL": dsn}, primary=PRIMARY)
    assert sentinel not in "".join(traceback.format_exception(error.value))


def test_strict_agent_dsn_requires_verified_tls(tmp_path: Path) -> None:
    base = "postgresql://clinic_agent:synthetic@127.0.0.1:5433/synthetic"
    with pytest.raises(ImproperlyConfigured):
        agent_database_config(
            {"AGENT_DATABASE_URL": base}, primary=PRIMARY, strict_tls=True
        )
    ca = tmp_path / "synthetic-ca.pem"
    ca.write_text("synthetic CA fixture\n")
    alias = agent_database_config(
        {
            "AGENT_DATABASE_URL": (
                f"{base}?sslmode=verify-full&connect_timeout=2&sslrootcert={ca}"
            )
        },
        primary=PRIMARY,
        strict_tls=True,
    )
    assert alias["agent"]["OPTIONS"] == {
        "sslmode": "verify-full",
        "connect_timeout": 2,
        "sslrootcert": str(ca),
        "options": "-c search_path=clinic_app,public",
    }
