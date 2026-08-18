"""Fail closed unless the process uses the locked timezone-data wheel."""

from __future__ import annotations

import os
import zoneinfo
from importlib.metadata import PackageNotFoundError, version
from typing import Final

TZDATA_VERSION: Final = "2026.3"


def enforce_wheel_timezone() -> None:
    """Reset timezone discovery to the exact locked wheel before app imports."""
    os.environ["PYTHONTZPATH"] = ""
    zoneinfo.reset_tzpath()
    try:
        installed = version("tzdata")
    except PackageNotFoundError as error:
        message = "timezone source contract failed"
        raise _TimezoneSourceError(message) from error
    if installed != TZDATA_VERSION or zoneinfo.TZPATH != ():
        message = "timezone source contract failed"
        raise _TimezoneSourceError(message)


class _TimezoneSourceError(RuntimeError):
    pass
