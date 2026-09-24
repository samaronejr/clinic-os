from __future__ import annotations

import importlib
import importlib.util
import os
import subprocess
import sys

import pytest
from django.core.exceptions import ImproperlyConfigured

_run_process = subprocess.run


def test_only_synthetic_data_mode_can_start() -> None:
    module_name = "config.settings.contracts"
    assert importlib.util.find_spec(module_name) is not None
    contracts = importlib.import_module(module_name)
    require_mode = getattr(contracts, "require_synthetic_mode", None)
    assert callable(require_mode)
    assert require_mode("synthetic") == "synthetic"
    with pytest.raises(ImproperlyConfigured, match="synthetic"):
        require_mode("live")


def test_data_mode_contract_keeps_synthetic_default_and_gates_live() -> None:
    """The startup gate: synthetic always starts, live needs an activation.

    ``require_data_mode`` replaced the unconditional live rejection with a
    stronger contract: live startup is authorized only by a valid activation
    record bound to the release, environment, evidence and storage (see
    tests/renewal/test_live_activation.py for the authorized transition).
    """
    contracts = importlib.import_module("config.settings.contracts")
    require_data_mode = getattr(contracts, "require_data_mode", None)
    assert callable(require_data_mode)
    assert require_data_mode("synthetic") == "synthetic"
    with pytest.raises(ImproperlyConfigured, match="CLINIC_DATA_MODE"):
        require_data_mode("bogus")
    with pytest.raises(ImproperlyConfigured, match="live data mode is not approved"):
        require_data_mode("live", {})


def test_unapproved_live_startup_fails_closed() -> None:
    """CLINIC_DATA_MODE=live without a valid activation never starts."""
    environment = os.environ.copy()
    environment.update(
        {
            "CLINIC_DATA_MODE": "live",
            "DJANGO_SETTINGS_MODULE": "config.settings.test",
        }
    )
    result = _run_process(
        (sys.executable, "-c", "import config.settings.base"),
        check=False,
        capture_output=True,
        env=environment,
        text=True,
    )
    assert result.returncode != 0
    assert "live data mode is not approved" in result.stderr
