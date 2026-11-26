"""Payload-free observations on an owned posture test cluster and child group."""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import psycopg


def activity(db: psycopg.Connection) -> list[dict[str, object]]:
    rows = db.execute(
        """
        SELECT CASE usename WHEN 'clinic_app' THEN 'app' WHEN 'clinic_posture' THEN
                 'inspector' ELSE 'other' END, state, wait_event_type, wait_event,
               count(*), max(EXTRACT(EPOCH FROM(clock_timestamp()-xact_start))*1000),
               count(*) FILTER(WHERE cardinality(pg_blocking_pids(pid))>0)
        FROM pg_catalog.pg_stat_activity
        WHERE backend_type='client backend' AND pid<>pg_backend_pid()
        GROUP BY 1,2,3,4 ORDER BY 1,2,3,4
        """
    ).fetchall()
    return [
        {
            "role": role,
            "state": state,
            "wait_type": kind,
            "wait": event,
            "count": count,
            "oldest_tx_ms": int(age or 0),
            "blocked": blocked,
        }
        for role, state, kind, event, count, age, blocked in rows
    ]


def checkpoint(db: psycopg.Connection, stage: str) -> dict[str, object]:
    return {"ns": time.monotonic_ns(), "stage": stage, "backends": activity(db)}


def process_group(root: int) -> list[dict[str, object]]:
    """Read only process class, scheduling state and counters, never argv or SQL."""
    rows: list[dict[str, object]] = []
    pending = [root]
    seen: set[int] = set()
    while pending:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        proc = Path("/proc") / str(pid)
        try:
            fields = (proc / "stat").read_text().rsplit(")", 1)[1].split()
            if int(fields[2]) != root:
                continue
            name = (proc / "comm").read_text().strip()
            rows.append(
                {
                    "process": name
                    if name
                    in {"make", "sh", "bash", "uv", "python", "python3", "python3.12"}
                    else "other",
                    "state": fields[0],
                    "wchan": (proc / "wchan").read_text().strip(),
                    "major_faults": int(fields[9]),
                    "cpu_ticks": int(fields[11]) + int(fields[12]),
                }
            )
            pending.extend(
                int(x)
                for x in (proc / "task" / str(pid) / "children").read_text().split()
            )
        except (FileNotFoundError, ProcessLookupError):
            continue
    return rows
