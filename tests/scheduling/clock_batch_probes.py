"""One catalog closure per complete plant family, with every runtime oracle kept."""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager
from dataclasses import replace
from typing import TYPE_CHECKING

import psycopg

from .clock_catalog import live_clock_inventory
from .clock_probe_catalog import catalog_sha256
from .test_residual_clock_probes import RESTORATION
from .test_scheduling_clock_inventory import (
    test_all_live_scheduling_sql_time_reads_are_controlled as assert_inventory,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    import pytest

    from .clock_catalog import ClockNode
    from .clock_residual_probes import Probe


@contextmanager
def installed_family(
    database_url: str, cases: dict[str, Probe], request: pytest.FixtureRequest
) -> Iterator[tuple[dict[str, ClockNode], dict[str, str]]]:
    before = live_clock_inventory()
    fingerprint = catalog_sha256()
    receipt = {"catalog_before_sha256": fingerprint}
    request.node.stash[RESTORATION] = receipt
    installed = []
    keys = {}
    runtime = {}
    with psycopg.connect(database_url, autocommit=True) as owner:
        try:
            for index, (name, original) in enumerate(cases.items()):
                root = "scheduling_r8_batch_" + str(index)

                def translate(source: str, index: int = index, root: str = root) -> str:
                    return re.sub(r"\br8\b", "r8_batch_" + str(index), source).replace(
                        "scheduling_r8_root", root
                    )

                probe = replace(
                    original,
                    install=translate(original.install),
                    remove=translate(original.remove),
                    runtime=translate(original.runtime),
                )
                owner.execute(probe.install.encode())
                installed.append(probe)
                actual = owner.execute(probe.runtime.encode()).fetchone()
                runtime[name] = repr(actual)
                assert actual == probe.expected, name
                keys[name] = "function:clinic_app." + root + "()"
            receipt["planted_sql_sha256"] = hashlib.sha256(
                "".join(p.install for p in installed).encode()
            ).hexdigest()
            receipt["runtime_cells"] = json.dumps(runtime, sort_keys=True)
            assert set(runtime) == set(cases)
            yield live_clock_inventory(), keys
        finally:
            for probe in reversed(installed):
                owner.execute(probe.remove.encode())
            restored = catalog_sha256()
            receipt["catalog_after_sha256"] = restored
            assert restored == fingerprint
            assert live_clock_inventory() == before
            assert_inventory()
