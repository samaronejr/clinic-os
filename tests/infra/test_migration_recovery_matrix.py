"""Exercise independent copies of real interrupted upgrades, all the way to head."""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING, NamedTuple

import pytest
from apps.tenancy.migrations._resume_catalog import ResumeCatalogError, history
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.loader import MigrationLoader

from infra.migration_cluster_state import (
    assert_unchanged,
    logical_state,
    observe_resume_sql,
)
from infra.migration_matrix_catalog import (
    TAMPERS,
    cluster_state,
    heaps,
    maintain,
    restore_designation,
    schema_hash,
)
from infra.test_migration_recovery_r8 import _head_predecessor, _to_head
from infra.test_protected_migration_resume import (
    SimulatedInterruptionError,
    _apply_with_cut,
    _clone,
)
from patients.test_intake_migrations import _default_connection, _scratch_database

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

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


def _refuses(db: DatabaseWrapper, case: Case, named: str) -> None:
    db.close()
    assert case.target not in MigrationExecutor(db).loader.applied_migrations
    with pytest.raises(ResumeCatalogError, match=re.escape(named)):
        _to_head(db)
    db.close()
    assert case.target not in MigrationExecutor(db).loader.applied_migrations


def _check_cluster(
    db: DatabaseWrapper,
    case: Case,
    before: dict[str, tuple[int, str | None]],
    negative: Sequence[str],
    schema: str,
) -> Iterator[tuple[str, str]]:
    tables = list(before)
    after = cluster_state(db, tables)
    for name in tables:
        assert before[name][0] != after[name][0], name
    assert all(after[name][1] is not None for name in tables)
    changed = [name for name in tables if before[name][1] != after[name][1]]
    # The selected index is already preferred when it has a designation. Never
    # clear an operator's existing designation merely because CLUSTER was run.
    assert all(before[name][1] is None and after[name][1] for name in changed)
    admin = os.environ["TEST_SUPERUSER_DATABASE_URL"]
    if changed:
        error = _bare_refusal(db, case)
        prefix = ".".join(case.target) + " cannot resume: conflicting catalog for "
        assert error.startswith(prefix)
        field = "pg_index.indisclustered (differs)"
        assert set(error.removeprefix(prefix).split(", ")) == {
            f"relation {name}.{after[name][1]}.{field}" for name in changed
        }
        yield "bare-cluster-refused", error
    for kind in negative:
        with _clone(admin, db) as altered:
            named = TAMPERS[kind](altered, tables)
            if changed:
                # The bare error can report the designation before the second
                # fault. Clear *only* that designation and require the planted
                # object to be named on this same still-tampered database.
                _bare_refusal(altered, case)
                yield "bare-" + kind, named
                for name in changed:
                    restore_designation(altered, name, None)
            _refuses(altered, case, named)
        yield kind, named
    # Do not use a pristine sibling as the recovery proof. This is the exact
    # database that refused, with the same rewritten heap and original rows.
    for name in changed:
        restore_designation(db, name, None)
    assert cluster_state(db, tables) == {
        name: (after[name][0], before[name][1]) for name in tables
    }
    _to_head(db)
    actual = schema_hash(db)
    assert actual == schema
    yield "head-schema", actual


def _bare_refusal(db: DatabaseWrapper, case: Case) -> str:
    before = logical_state(db)
    db.close()
    with observe_resume_sql(db) as writes, pytest.raises(ResumeCatalogError) as error:
        _to_head(db)
    db.close()
    assert_unchanged(before, logical_state(db))
    assert not writes, f"Refusal attempted migration work: {writes}"
    assert case.target not in MigrationExecutor(db).loader.applied_migrations
    return str(error.value)


def _exercise_cut(
    cut: DatabaseWrapper,
    case: Case,
    maintenance: str,
    negative: Sequence[str],
    schema: str,
) -> Iterator[tuple[str, str]]:
    tables = [name for name, _ in heaps(cut, list(case.tables))]
    before = cluster_state(cut, tables) if maintenance == "CLUSTER" else {}
    applied = maintain(cut, tables, maintenance)
    if not applied:
        # No clusterable index exists at this cut. Every other operation must
        # have an actual target; do not count this cut as a CLUSTER success.
        assert maintenance == "CLUSTER"
        assert all(index is None for _, index in heaps(cut, tables))
        return
    if maintenance == "CLUSTER":
        yield from _check_cluster(
            cut, case, {name: before[name] for name in applied}, negative, schema
        )
        return
    admin = os.environ["TEST_SUPERUSER_DATABASE_URL"]
    # Establish that the unaltered sibling succeeds before crediting a refusal.
    # A failure caused by maintenance itself is NOT a passing tamper control.
    with _clone(admin, cut) as positive:
        _to_head(positive)
        actual = schema_hash(positive)
        assert actual == schema, (case.target, maintenance, applied)
    yield "head-schema", actual
    for kind in negative:
        with _clone(admin, cut) as altered:
            named = TAMPERS[kind](altered, tables)
            _refuses(altered, case, named)
        yield kind, named


def test_resume_maintenance_matrix(
    matrix_case: Case,
    maintenance: str,
    clean_head_schema: str,
    django_db_blocker: DjangoDbBlocker,
    request: pytest.FixtureRequest,
) -> None:
    admin = os.environ["TEST_SUPERUSER_DATABASE_URL"]
    full = request.config.getoption("--full-recovery-matrix")
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
        bare_refused = []
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
                for check, value in _exercise_cut(
                    cut, matrix_case, maintenance, negative, clean_head_schema
                ):
                    request.node.user_properties.append((f"cut-{stop}.{check}", value))
                    if check == "bare-cluster-head":
                        assert value == clean_head_schema
                    elif check == "bare-cluster-refused":
                        bare_refused.append(stop)
        # No other maintenance operation may be credited for a refusal.
        assert not bare_refused or maintenance == "CLUSTER"
