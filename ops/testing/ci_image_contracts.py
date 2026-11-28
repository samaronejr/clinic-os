"""Run the stand-alone image contract test gate."""

from __future__ import annotations

import pytest

from ops.testing.pidfd_preflight import require_pidfd_at_startup


def main() -> int:
    """Run the fixed image contract suite after checking interpreter support."""
    require_pidfd_at_startup()
    return pytest.main(["-q", "tests/isolation/test_container_contract.py"])


if __name__ == "__main__":
    raise SystemExit(main())
