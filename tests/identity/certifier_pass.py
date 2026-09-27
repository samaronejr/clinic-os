"""One sharded matrix pass for every property certified over all states.

Three tests certify properties over the whole staff-state matrix:

- every census exemption is staff independent and never observes the actor
  (``test_every_exemption_probe_is_staff_independent``);
- a permission-gated function is classified gated, and succeeds exactly
  where the live decision grants (``test_differential_probe_...``: the
  ``set_intake_policy`` probe and its oracle);
- every R4/R5 gate variant of the registered ``_next_version`` probe is
  refused (``test_exemption_probes_refuse_every_r4_gate_variant``).

Each used to seed its own world and run its own pass. They now share one
pass over one world clone (identity/probe_worlds.py): the union of their
probes (the registered ``_next_version`` probe is in the census set once),
sharded like any matrix run (identity/probe_shards.py). A probe's run
depends only on the probe and the state: every probe still executes exactly
once per state, into its own outcome, observer sink and reach, and each test
reads the runs of its own probes. The pass is computed by the first of the
three tests that asks for it and kept for the session; its runs are read,
never changed.
"""

from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter
from typing import TYPE_CHECKING, Final

from apps.intake import demographics
from django.db import connection

from identity import exemption_probes, probe_shards, probe_states, probe_worlds

if TYPE_CHECKING:
    from collections.abc import Mapping

POLICY: Final = "apps.intake.demographics.set_intake_policy"
ORACLE: Final = f"{POLICY}#oracle"


def configuration_answer(pw: exemption_probes.ProbeWorld) -> bool:
    """The live decision set_intake_policy asks, read as data."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.has_permission('configuration.clinic', %s, NULL) "
            "OR clinic_app.has_permission('configuration.organization', %s, NULL)",
            [pw.w.clinic, pw.w.clinic],
        )
        row = cursor.fetchone()
    return bool(row and row[0])


def gated_probes() -> tuple[exemption_probes.ExemptionProbe, ...]:
    """The gated function and the oracle that reads its decision."""
    return (
        exemption_probes.ExemptionProbe(
            POLICY,
            "staff",
            "success",
            lambda pw: demographics.set_intake_policy(
                clinic_id=pw.w.clinic, required_fields=[]
            ),
        ),
        exemption_probes.ExemptionProbe(
            ORACLE,
            "staff",
            "success",
            configuration_answer,
            code=configuration_answer.__code__,
        ),
    )


@dataclass(frozen=True, slots=True)
class Pass:
    """The shared pass: every probe by symbol, its runs, and the matrix
    receipts (rows read back where they were written, shard wall times)."""

    probes: Mapping[str, exemption_probes.ExemptionProbe]
    runs: Mapping[str, exemption_probes.ProbeRun]
    realized: Mapping[str, set[str]]
    shard_seconds: tuple[float, ...]
    run_seconds: float


_PASSES: dict[str, Pass] = {}


def census_probes() -> list[exemption_probes.ExemptionProbe]:
    return [exemption_probes.PROBES[key] for key in sorted(exemption_probes.PROBES)]


def probes_for(
    world: exemption_probes.ProbeWorld,
) -> list[exemption_probes.ExemptionProbe]:
    """The union of the three tests' probes, each symbol once."""
    from identity.test_exemption_probe_soundness import (  # noqa: PLC0415 - that test module imports this one
        _variant_probes,
    )

    variants = _variant_probes(world)
    registered = variants[0]
    assert registered is exemption_probes.PROBES[registered.symbol]
    probes = [*census_probes(), *gated_probes(), *variants[1:]]
    symbols = [probe.symbol for probe in probes]
    assert len(set(symbols)) == len(symbols), "a probe symbol twice in the pass"
    return probes


def full_pass(world: exemption_probes.ProbeWorld) -> Pass:
    """The session's shared pass over ``world`` (a fresh clone of the session
    template, as every caller holds), run on first use."""
    key = probe_worlds.template_now().name
    if key not in _PASSES:
        probes = probes_for(world)
        started = perf_counter()
        runs = exemption_probes.run_matrix(
            probes, world, workers=probe_shards.worker_count()
        )
        _PASSES[key] = Pass(
            {probe.symbol: probe for probe in probes},
            runs,
            probe_states.realized(world.matrix),
            tuple(world.matrix.shard_seconds),
            round(perf_counter() - started, 1),
        )
    return _PASSES[key]
