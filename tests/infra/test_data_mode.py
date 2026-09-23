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
    assert "synthetic data mode is required" in result.stderr
