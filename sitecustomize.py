"""Apply process-wide runtime invariants before project imports."""

from config.runtime import enforce_wheel_timezone

enforce_wheel_timezone()
