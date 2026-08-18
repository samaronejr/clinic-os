"""Expose the two closed durable final-review lane invocations."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Never

if __package__ in {None, ""}:
    _ROOT = Path(__file__).resolve().parents[2]
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))

from ops.testing.isolation_common import IsolationError
from ops.testing.isolation_review_orchestrator import (
    ReviewControllerRequest,
    run_review_controller,
)

if TYPE_CHECKING:
    from ops.testing.isolation_review_lane_record import ReviewLane

EXPECTED_INPUTS = Path(".omo/evidence/clinic-os-phase1a-final/terminal/inputs.json")
SHA40 = re.compile(r"^[0-9a-f]{40}$")
__all__ = ["EXPECTED_INPUTS", "ReviewControllerRequest", "main"]


def main() -> int:
    """Parse one exact F1/F2 form and run its durable controller."""
    arguments = _parser().parse_args()
    if arguments.inputs != EXPECTED_INPUTS or SHA40.fullmatch(arguments.sha) is None:
        _fail("review controller inputs path is not the fixed final manifest")
    lane: ReviewLane = "F1" if arguments.lane == "F1" else "F2"
    try:
        return run_review_controller(
            ReviewControllerRequest(lane, arguments.sha, arguments.inputs.resolve())
        )
    except (IsolationError, OSError, ValueError, KeyError) as error:
        sys.stderr.write(f"final-review-controller: {error}\n")
        return 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("lane", choices=("F1", "F2"))
    parser.add_argument("--sha", required=True)
    parser.add_argument("--inputs", required=True, type=Path)
    return parser


def _fail(message: str) -> Never:
    raise IsolationError(message)


if __name__ == "__main__":
    raise SystemExit(main())
