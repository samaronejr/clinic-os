"""Exercise independent copies of real interrupted upgrades, all the way to head."""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING, NamedTuple

import pytest
from apps.tenancy.migrations._resume_catalog import ResumeCatalogError, history
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.loader import MigrationLoader

from infra.migration_matrix_catalog import TAMPERS, heaps, maintain, schema_hash
from infra.test_migration_recovery_r8 import _head_predecessor, _to_head
from infra.test_protected_migration_resume import (
    SimulatedInterruptionError,
    _apply_with_cut,
    _clone,
)
from patients.test_intake_migrations import _default_connection, _scratch_database

if TYPE_CHECKING:
    from collections.abc import Sequence

    from django.db.backends.postgresql.base import DatabaseWrapper
    from pytest_django.plugin import DjangoDbBlocker

OPERATIONS = ("VACUUM", "REINDEX", "CLUSTER", "ANALYZE")


class Case(NamedTuple):
    target: tuple[str, str]
    position: int
    tables: tuple[str, ...]


def _cases() -> list[Case]:
    graph = MigrationLoader(None).graph
    ordered = dict.fromkeys(
        k for leaf in graph.leaf_nodes() for k in graph.forwards_plan(leaf)
    )
    guarded = {
        name: sorted(
            {
                obj.removeprefix("relation ")
                for step, delta in operations
                if step is not None
                for obj in delta
                if obj.startswith("relation ")
            }
        )
        for name, operations in history().items()
        if any(s is not None for s, _ in operations)
    }
    keys = [k for k in ordered if ".".join(k) in guarded]
    assert {".".join(k) for k in keys} == guarded.keys()
    assert keys
    return [Case(k, idx, tuple(guarded[".".join(k)])) for idx, k in enumerate(keys)]


@pytest.fixture(scope="module")
def clean_head_schema(django_db_blocker: DjangoDbBlocker) -> str:
    with (
        django_db_blocker.unblock(),
        _scratch_database(os.environ["TEST_SUPERUSER_DATABASE_URL"]) as db,
        _default_connection(db),
    ):
        _to_head(db)
        return schema_hash(db)


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "matrix_case" not in metafunc.fixturenames:
        return
    full = metafunc.config.getoption("--full-recovery-matrix")
    items = [
        pytest.param(case, op, id=".".join(case.target) + "-" + op.lower())
        for case in _cases()
        for op in (
            OPERATIONS if full else (OPERATIONS[case.position % len(OPERATIONS)],)
        )
    ]
    metafunc.parametrize(("matrix_case", "maintenance"), items)


def _reject_mutant(
    db: DatabaseWrapper, case: Case, tables: list[str], kind: str
) -> None:
    named = TAMPERS[kind](db, tables)
    db.close()
    assert case.target not in MigrationExecutor(db).loader.applied_migrations
    with pytest.raises(ResumeCatalogError, match=re.escape(named)):
        _to_head(db)
    db.close()
    assert case.target not in MigrationExecutor(db).loader.applied_migrations


def _exercise_cut(
    cut: DatabaseWrapper,
    case: Case,
    maintenance: str,
    negative: Sequence[str],
    schema: str,
) -> None:
    tables = [name for name, _ in heaps(cut, list(case.tables))]
    applied = maintain(cut, tables, maintenance)
    if not applied:
        # No clusterable index exists at this cut. Every other operation must
        # have an actual target; do not count this cut as a CLUSTER success.
        assert maintenance == "CLUSTER"
        assert all(index is None for _, index in heaps(cut, tables))
        return
    admin = os.environ["TEST_SUPERUSER_DATABASE_URL"]
    # Establish that the unaltered sibling succeeds before crediting a refusal.
    # A failure caused by maintenance itself is NOT a passing tamper control.
    with _clone(admin, cut) as positive:
        _to_head(positive)
        assert schema_hash(positive) == schema, (case.target, maintenance, applied)
    for kind in negative:
        with _clone(admin, cut) as altered:
            _reject_mutant(altered, case, tables, kind)


def test_resume_maintenance_matrix(
    matrix_case: Case,
    maintenance: str,
    clean_head_schema: str,
    django_db_blocker: DjangoDbBlocker,
    pytestconfig: pytest.Config,
) -> None:
    admin = os.environ["TEST_SUPERUSER_DATABASE_URL"]
    full = pytestconfig.getoption("--full-recovery-matrix")
    kinds = tuple(TAMPERS)
    with (
        django_db_blocker.unblock(),
        _scratch_database(admin) as prior,
        _default_connection(prior),
        pytest.MonkeyPatch.context() as monkeypatch,
    ):
        _head_predecessor(prior, matrix_case.target)
        with _clone(admin, prior) as baseline:
            cuts = _apply_with_cut(baseline, matrix_case.target, monkeypatch)
        assert cuts, matrix_case.target
        for stop, label in enumerate(cuts, 1):
            with _clone(admin, prior) as cut:
                with pytest.raises(SimulatedInterruptionError, match=re.escape(label)):
                    _apply_with_cut(cut, matrix_case.target, monkeypatch, stop)
                cut.close()
                assert (
                    matrix_case.target
                    not in MigrationExecutor(cut).loader.applied_migrations
                )
                negative = (
                    kinds
                    if full
                    else (kinds[(matrix_case.position + stop - 1) % len(kinds)],)
                )
                _exercise_cut(
                    cut, matrix_case, maintenance, negative, clean_head_schema
                )
