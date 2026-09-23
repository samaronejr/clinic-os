from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from ops.testing.isolation_common import IsolationError
from ops.testing.validate_tdd_receipt import (
    ReceiptSourceLocations,
    validate_receipt_sources,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_receipt_source_resolver_requires_exactly_twenty_primaries(
    tmp_path: Path,
) -> None:
    def unused_git(*_arguments: str) -> bytes:
        message = "Git must not run before primary-set validation"
        raise AssertionError(message)

    with pytest.raises(IsolationError, match="exactly todos 1 through 20"):
        validate_receipt_sources(
            ReceiptSourceLocations(
                tmp_path,
                tmp_path / "attempt",
                "11111111-1111-4111-8111-111111111111",
            ),
            [],
            [],
            "a" * 40,
            unused_git,
        )
