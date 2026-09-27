"""Bounded profile workers over identical snapshots in the lane PostgreSQL.

Fork preserves real adapters and fixed inputs. Each worker owns one database,
not a container. Disk pressure reduces subsequent launches to one worker.
"""

from __future__ import annotations

import multiprocessing
import os
import shutil
import traceback
from multiprocessing.connection import wait
from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import psycopg
from django.db import connections
from django.db.backends.postgresql.base import DatabaseWrapper
from psycopg import sql

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from multiprocessing.connection import Connection
    from multiprocessing.process import BaseProcess

    from identity.nonstaff_differential import DifferentialReport

Profile = tuple[str, str, str]
WORKERS = 4
MIN_FREE_BYTES = 30 * 1024**3


def _worker(
    send: Connection,
    database: str,
    run: Callable[[tuple[Profile, ...]], DifferentialReport],
    group: tuple[Profile, ...],
) -> None:
    settings = connections["default"].settings_dict.copy()
    settings["NAME"] = database
    connections["default"] = DatabaseWrapper(settings, alias="default")
    try:
        send.send(run(group))
    except BaseException:
        send.send(traceback.format_exc())
        raise
    finally:
        connections.close_all()
        send.close()


def _database(command: str, name: str, source: str) -> None:
    with psycopg.connect(
        os.environ["TEST_SUPERUSER_DATABASE_URL"], autocommit=True
    ) as admin:
        if command == "create":
            admin.execute(
                sql.SQL(
                    "CREATE DATABASE {} WITH TEMPLATE {} OWNER clinic_owner"
                ).format(sql.Identifier(name), sql.Identifier(source))
            )
        else:
            assert command == "drop"
            admin.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )


def replay_profiles(
    groups: Sequence[Profile],
    run: Callable[[tuple[Profile, ...]], DifferentialReport],
) -> tuple[list[DifferentialReport], float]:
    start = perf_counter()
    source = connections["default"].settings_dict["NAME"]
    connections.close_all()
    context = multiprocessing.get_context("fork")
    cwd = Path.cwd().resolve()
    pending: dict[Connection, tuple[BaseProcess, str, int]] = {}
    databases: set[str] = set()
    results: dict[int, DifferentialReport] = {}
    next_index = 0
    process: BaseProcess
    try:
        while next_index < len(groups) or pending:
            limit = WORKERS if shutil.disk_usage(cwd).free >= MIN_FREE_BYTES else 1
            while next_index < len(groups) and len(pending) < limit:
                name = "t6_matrix_" + uuid4().hex
                _database("create", name, source)
                databases.add(name)
                receive, send = context.Pipe(duplex=False)
                process = context.Process(
                    target=_worker, args=(send, name, run, (groups[next_index],))
                )
                # Both Django and administrative DB sockets are closed before fork.
                process.start()
                send.close()
                pending[receive] = process, name, next_index
                next_index += 1
            ready = cast("list[Connection]", wait(pending, timeout=3600))
            assert ready, "differential worker deadline exceeded"
            for receive in ready:
                process, name, index = pending[receive]
                result = receive.recv()
                assert not isinstance(result, str), result
                process.join(timeout=30)
                assert process.exitcode == 0, process.exitcode
                results[index] = cast("DifferentialReport", result)
                receive.close()
                del pending[receive]
                _database("drop", name, source)
                databases.remove(name)
        assert set(results) == set(range(len(groups)))
        return [results[index] for index in range(len(groups))], perf_counter() - start
    finally:
        for receive, (process, _name, _index) in pending.items():
            if process.is_alive():
                assert process.pid is not None
                assert Path(f"/proc/{process.pid}/cwd").resolve() == cwd
                process.terminate()
                process.join(timeout=30)
                if process.is_alive():
                    process.kill()
                    process.join(timeout=30)
                assert not process.is_alive()
            receive.close()
        for name in databases:
            _database("drop", name, source)
