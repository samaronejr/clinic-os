"""Options for PostgreSQL migration recovery tests."""

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.getgroup("migration recovery").addoption(
        "--full-recovery-matrix",
        action="store_true",
        help="Exercise every maintenance operation at every protected commit.",
    )
