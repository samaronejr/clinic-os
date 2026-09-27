"""Run the probe matrix's states across worker processes, each on its own clone.

The matrix executes every exemption probe under every staff state, once; no
state is sampled. Sharding only changes where a state runs:

- the states after the first (the first runs in the parent, traced by
  coverage) are split into contiguous shards, at most ``WORKERS`` (the hosted
  runner's vCPUs), and the phase tail (states whose setup commits shared
  rows, which must run after every committed state and in order) stays whole
  in the last shard;
- each shard runs in a forked worker (fork keeps the world, the probes and
  the observer exactly as built) on its own database, cloned from the test
  database with ``CREATE DATABASE ... TEMPLATE`` after the parent's own
  runs, so every worker starts from the database the serial run would
  continue on; the clone gets the source's database ACL back (a template
  copy would get the default one) and must match the parent's session
  environment (settings, roles, catalog), or the run fails;
- a worker opens its own connection, enables function statistics on it and
  runs the observer there, so the per-execution session reads (temporary
  schema, prepared statements, standard_conforming_strings) and the wire
  trace are of the connection that runs the state.

It fails closed: a partition that misses, duplicates or leaves out a state,
an empty shard, a worker that crashes, raises, returns nothing or returns
other states than it was given, all fail the run. Every clone is dropped and
every worker reaped on success and on failure.
"""

from __future__ import annotations

import multiprocessing
import os
import threading
import traceback
from itertools import pairwise
from multiprocessing.connection import wait
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast
from uuid import uuid4

import psycopg
from django.db import connection, connections
from django.db.backends.postgresql.base import DatabaseWrapper
from psycopg import sql

from database_urls import database_url_for_name
from identity import actor_channels

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from multiprocessing.connection import Connection
    from multiprocessing.process import BaseProcess
    from multiprocessing.synchronize import Lock

    from identity.probe_states import ProbeState

# The hosted runner has 4 vCPUs: more workers would only hide its behaviour.
WORKERS: Final = 4
CLONE_PREFIX: Final = "probe_shard_"
# A worker that has not answered by then is killed and the run fails.
DEADLINE_SECONDS: Final = 3600
_REAP_SECONDS: Final = 30

# (labels the worker ran, in order; its payload)
type ShardResult[P] = tuple[tuple[str, ...], P]


class ShardError(AssertionError):
    """A sharded run that cannot stand for the serial one."""


def worker_count() -> int:
    """Workers for the matrix: the hosted runner's vCPUs, or fewer."""
    return min(WORKERS, os.cpu_count() or 1)


def partition(
    states: Sequence[ProbeState], workers: int
) -> tuple[tuple[ProbeState, ...], ...]:
    """Contiguous shards of near-equal size; the phase tail stays whole in
    the last shard (a phase setup commits rows every later state sees)."""
    tail = next(
        (index for index, state in enumerate(states) if state.setup is not None),
        len(states),
    )
    count = max(1, min(workers, len(states)))
    bounds = [min(index * len(states) // count, tail) for index in range(count)]
    bounds.append(len(states))
    return tuple(
        tuple(states[start:end]) for start, end in pairwise(bounds) if end > start
    )


def check_partition(
    states: Sequence[ProbeState], shards: Sequence[Sequence[ProbeState]]
) -> None:
    """Every state in exactly one non-empty shard; the phase tail in order
    at the end of one shard."""
    labels = [state.label for state in states]
    placed = [state.label for shard in shards for state in shard]
    if not shards or any(not shard for shard in shards):
        message = "a shard holds no state"
        raise ShardError(message)
    duplicated = sorted({label for label in placed if placed.count(label) > 1})
    if duplicated:
        message = f"states placed in more than one shard: {duplicated}"
        raise ShardError(message)
    missing = sorted(set(labels) - set(placed))
    extra = sorted(set(placed) - set(labels))
    if missing or extra:
        message = f"the partition misses {missing} and adds {extra}"
        raise ShardError(message)
    phases = [state.label for state in states if state.setup is not None]
    if phases:
        tail = labels[labels.index(phases[0]) :]
        if [state.label for state in shards[-1]][-len(tail) :] != tail:
            message = "the phase tail must run whole, in order, at the end"
            raise ShardError(message)


type Environment = tuple[tuple[tuple[str, str], ...], tuple[object, ...]]


def environment() -> Environment:
    """What a probe on this connection could observe about its session and
    database besides the rows: non-default settings, roles, the database ACL
    and settings, the catalog's shape, and the session state the observer
    reads per execution."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT name, setting FROM pg_catalog.pg_settings "
            "WHERE source NOT IN ('default', 'override') ORDER BY name"
        )
        settings = tuple((str(name), str(value)) for name, value in cursor.fetchall())
        cursor.execute(
            "SELECT session_user, current_user, "
            "pg_catalog.current_setting('standard_conforming_strings'), "
            "(SELECT d.datacl::text FROM pg_catalog.pg_database d "
            " WHERE d.datname = pg_catalog.current_database()), "
            "(SELECT pg_catalog.count(*) FROM pg_catalog.pg_db_role_setting s "
            " JOIN pg_catalog.pg_database d ON d.oid = s.setdatabase "
            " WHERE d.datname = pg_catalog.current_database()), "
            "(SELECT pg_catalog.count(*) || ':' || pg_catalog.max(oid::int8) "
            " FROM pg_catalog.pg_class), "
            "(SELECT pg_catalog.count(*) || ':' || pg_catalog.max(oid::int8) "
            " FROM pg_catalog.pg_proc), "
            "(SELECT pg_catalog.count(*) || ':' || pg_catalog.max(oid::int8) "
            " FROM pg_catalog.pg_operator), "
            "pg_catalog.pg_my_temp_schema(), "
            "(SELECT pg_catalog.count(*) FROM pg_catalog.pg_prepared_statements "
            " WHERE from_sql)"
        )
        state = cursor.fetchone()
    assert state is not None
    return (settings, tuple(state))


def _superuser_url(database: str) -> str:
    return database_url_for_name(os.environ["TEST_SUPERUSER_DATABASE_URL"], database)


def _clone(name: str, source: str) -> None:
    """A copy of ``source`` with its owner and database ACL."""
    with psycopg.connect(_superuser_url("postgres"), autocommit=True) as admin:
        row = admin.execute(
            "SELECT pg_catalog.pg_get_userbyid(datdba) FROM pg_catalog.pg_database "
            "WHERE datname = %s",
            [source],
        ).fetchone()
        assert row is not None, source
        admin.execute(
            sql.SQL("CREATE DATABASE {} WITH TEMPLATE {} OWNER {}").format(
                sql.Identifier(name), sql.Identifier(source), sql.Identifier(row[0])
            )
        )
        admin.execute(
            sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(
                sql.Identifier(name)
            )
        )
        grants = admin.execute(
            "SELECT pg_catalog.pg_get_userbyid(a.grantee), a.privilege_type, "
            "a.is_grantable FROM pg_catalog.pg_database d, "
            "pg_catalog.aclexplode(d.datacl) a "
            "WHERE d.datname = %s AND a.grantee <> d.datdba ORDER BY 1, 2",
            [source],
        ).fetchall()
        for grantee, privilege, grantable in grants:
            admin.execute(
                sql.SQL("GRANT {} ON DATABASE {} TO {}{}").format(
                    sql.SQL(privilege),
                    sql.Identifier(name),
                    sql.Identifier(grantee),
                    sql.SQL(" WITH GRANT OPTION" if grantable else ""),
                )
            )
        acls = admin.execute(
            "SELECT datname, datacl::text FROM pg_catalog.pg_database "
            "WHERE datname IN (%s, %s)",
            [source, name],
        ).fetchall()
    if len({acl for _, acl in acls}) != 1:
        message = f"the clone's database ACL differs from the source: {acls}"
        raise ShardError(message)


def _drop(name: str) -> None:
    with psycopg.connect(_superuser_url("postgres"), autocommit=True) as admin:
        admin.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(name)
            )
        )


def leftovers() -> list[tuple[str, int]]:
    """Clones still present, with the backends still connected to them."""
    with psycopg.connect(_superuser_url("postgres"), autocommit=True) as admin:
        rows = admin.execute(
            "SELECT d.datname, (SELECT pg_catalog.count(*) "
            " FROM pg_catalog.pg_stat_activity a WHERE a.datname = d.datname) "
            "FROM pg_catalog.pg_database d WHERE d.datname LIKE %s ORDER BY 1",
            [CLONE_PREFIX + "%"],
        ).fetchall()
        orphans = admin.execute(
            "SELECT pg_catalog.count(*) FROM pg_catalog.pg_stat_activity "
            "WHERE datname LIKE %s",
            [CLONE_PREFIX + "%"],
        ).fetchone()
    assert orphans is not None
    return [(str(name), int(count)) for name, count in rows] + (
        [("<backends>", int(orphans[0]))] if orphans[0] else []
    )


def _same_environment(expected: Environment) -> None:
    found = environment()
    if found != expected:
        message = f"worker environment differs: {found!r} != {expected!r}"
        raise ShardError(message)


def _worker[P](  # noqa: PLR0913 - one worker needs its whole context
    send: Connection,
    database: str,
    expected: Environment,
    lock: Lock,
    work: Callable[[tuple[ProbeState, ...]], ShardResult[P]],
    shard: tuple[ProbeState, ...],
) -> None:
    """One shard on its own clone and connection (runs in the fork)."""
    settings = connections["default"].settings_dict.copy()
    settings["NAME"] = database
    connections["default"] = DatabaseWrapper(settings, alias="default")
    try:
        with lock:  # GRANT/REVOKE SET ON PARAMETER is cluster-wide
            actor_channels.enable_function_statistics(_superuser_url(database))
        _same_environment(expected)
        send.send(("ok", work(shard)))
    except BaseException:
        send.send(("error", traceback.format_exc()))
        raise
    finally:
        connections.close_all()
        send.close()


def _preflight(
    states: Sequence[ProbeState], shards: Sequence[tuple[ProbeState, ...]]
) -> Environment:
    """What must hold before any fork: a valid partition, one thread, and a
    session a fresh worker connection reproduces."""
    check_partition(states, shards)
    if threading.active_count() != 1:
        message = "the matrix forks its workers only from a single-threaded process"
        raise ShardError(message)
    expected = environment()
    if expected[1][-2:] != (0, 0):
        message = (
            "the session holds temporary objects or SQL prepared statements a "
            "fresh worker connection would not"
        )
        raise ShardError(message)
    return expected


def _accepted(
    index: int,
    reply: tuple[str, object],
    exitcode: int | None,
    shard: tuple[ProbeState, ...],
) -> object:
    status, value = reply
    if status != "ok" or exitcode != 0:
        message = f"shard {index} failed (exit code {exitcode}): {value}"
        raise ShardError(message)
    labels, payload = cast("ShardResult[object]", value)
    given = tuple(state.label for state in shard)
    if labels != given:
        message = (
            f"shard {index} ran {len(labels)} of its {len(given)} states, or others"
        )
        raise ShardError(message)
    return payload


def run_shards[P](
    states: Sequence[ProbeState],
    shards: Sequence[tuple[ProbeState, ...]],
    work: Callable[[tuple[ProbeState, ...]], ShardResult[P]],
) -> list[P]:
    """Run each shard in a forked worker on its own clone; the payloads come
    back in shard order. The caller's connection is closed first (a clone
    needs its template idle) and reopens on its next use."""
    expected = _preflight(states, shards)
    source = str(connections["default"].settings_dict["NAME"])
    connections.close_all()
    context = multiprocessing.get_context("fork")
    lock = context.Lock()
    cwd = Path.cwd().resolve()
    clones: list[str] = []
    pending: dict[Connection, tuple[BaseProcess, int]] = {}
    results: dict[int, object] = {}
    try:
        for _ in shards:
            clones.append(CLONE_PREFIX + uuid4().hex)
            _clone(clones[-1], source)
        for index, shard in enumerate(shards):
            receive, send = context.Pipe(duplex=False)
            worker: BaseProcess = context.Process(
                target=_worker,
                args=(send, clones[index], expected, lock, work, shard),
            )
            worker.start()
            send.close()
            pending[receive] = worker, index
        while pending:
            ready = cast("list[Connection]", wait(pending, timeout=DEADLINE_SECONDS))
            if not ready:
                message = "a shard worker missed the deadline"
                raise ShardError(message)
            for receive in ready:
                worker, index = pending.pop(receive)
                try:
                    reply = receive.recv()
                except EOFError:
                    reply = ("error", "the worker exited without a result")
                finally:
                    receive.close()
                worker.join(timeout=_REAP_SECONDS)
                results[index] = _accepted(index, reply, worker.exitcode, shards[index])
        return cast("list[P]", [results[index] for index in range(len(shards))])
    finally:
        for receive, (worker, _index) in pending.items():
            _reap(worker, cwd)
            receive.close()
        for name in clones:
            _drop(name)


def _reap(process: BaseProcess, cwd: Path) -> None:
    """Stop a worker this run started (never anything else)."""
    if process.is_alive():
        assert process.pid is not None
        assert Path(f"/proc/{process.pid}/cwd").resolve() == cwd
        process.terminate()
        process.join(timeout=_REAP_SECONDS)
        if process.is_alive():
            process.kill()
            process.join(timeout=_REAP_SECONDS)
    assert not process.is_alive()
