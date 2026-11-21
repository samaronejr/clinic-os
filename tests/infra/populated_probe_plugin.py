"""Opt-in execution of the exact synthetic scenarios in the populated probe map.

Run ``PYTHONPATH=tests python -m infra.populated_probe_plugin``. It resolves
fixture dependencies through pytest, not by copying positive test bodies or
changing any workflow's trigger/session requirements.
"""

from __future__ import annotations

import os
import sys
from collections import defaultdict
from importlib import import_module
from typing import TYPE_CHECKING, cast

import django
import pytest

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Iterable

    from infra.populated_probe_inventory import Probe


def _groups(inventory: tuple[Probe, ...]) -> dict[str, list[Probe]]:
    grouped: dict[str, list[Probe]] = defaultdict(list)
    for probe in inventory:
        if probe.scope != "shared":
            grouped[probe.seed].append(probe)
    return dict(grouped)


class PopulatedProbeCases:
    def __init__(
        self,
        groups: dict[str, list[Probe]],
        check: Callable[[Iterable[Probe]], None],
    ) -> None:
        self._groups = groups
        self._check = check
        self._verified: set[str] = set()

    def pytest_collection_finish(self, session: pytest.Session) -> None:
        actual = {item.nodeid for item in session.items}
        assert set(self._groups) == actual, (
            "probe seed selection drift",
            sorted(set(self._groups) - actual),
            sorted(actual - set(self._groups)),
        )

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_call(self, item: pytest.Item) -> Generator[None, object, object]:
        result = yield
        probes = self._groups[item.nodeid]
        self._check(probes)
        self._verified.update(probe.relation for probe in probes)
        return result

    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        required = {
            probe.relation for probes in self._groups.values() for probe in probes
        }
        missing = required - self._verified
        if missing and exitstatus == pytest.ExitCode.OK:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            reporter.write_line(
                f"Populated tenant probes: "
                f"{len(self._verified)}/{len(required)} verified"
            )
            if missing:
                reporter.write_line(
                    f"Unverified relations: {', '.join(sorted(missing))}"
                )


def main() -> int:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.test")
    django.setup()
    # Load ORM fixtures only once Django's app registry is ready.
    support = import_module("infra.populated_probe_inventory")
    groups = _groups(cast("tuple[Probe, ...]", support.inventory()))
    cases = PopulatedProbeCases(groups, support.check_seeded_relations)
    # One collection and normal pytest setup/teardown; no in-process reentry.
    return pytest.main([*sys.argv[1:], *groups], plugins=[cases])


if __name__ == "__main__":
    sys.exit(main())
