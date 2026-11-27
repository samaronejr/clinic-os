"""L5: compare committed state on an independent, privileged clone connection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import psycopg
from psycopg import sql

if TYPE_CHECKING:
    from datetime import datetime

    from django.db.backends.postgresql.base import DatabaseWrapper

type Relation = tuple[str, str, str, int | None]
type RecorderRow = tuple[int, str, str, datetime]
type SequenceState = tuple[int, bool]

_RECORDER = ("clinic_app", "django_migrations")
_RECORDER_ROW = sql.SQL(
    "SELECT id, app, name, applied FROM clinic_app.django_migrations WHERE "
)


def recorded_row(db: DatabaseWrapper, app: str, name: str) -> RecorderRow:
    """Identify the precise row written by the real Django recorder call."""
    with db.cursor() as cursor:
        cursor.execute(_RECORDER_ROW + sql.SQL("app = %s AND name = %s"), (app, name))
        (row,) = cursor.fetchall()
    return cast("RecorderRow", row)


@dataclass(frozen=True)
class CommittedData:
    rows: dict[int, str]
    sequences: dict[int, SequenceState]
    recorder_sequence: int | None
    recorder_entry_intact: bool

    def preserves(self, before: CommittedData, entry: RecorderRow) -> bool:
        # Django's recorder uses a noncycling increment-one, cache-one sequence.
        # An uncalled sequence returns its current value on the first nextval.
        sequence = before.recorder_sequence
        assert sequence is not None
        value, called = before.sequences[sequence]
        expected_id = value + int(called)
        return (
            self.recorder_entry_intact
            and self.recorder_sequence == sequence
            and entry[0] == expected_id
            and self.rows == before.rows
            and self.sequences == {**before.sequences, sequence: (expected_id, True)}
        )


def committed_data(
    clone_admin_url: str,
    relations: dict[int, Relation],
    entry: RecorderRow | None = None,
) -> CommittedData:
    """Read every old row multiset and sequence; never return record contents."""
    with psycopg.connect(clone_admin_url) as observer, observer.cursor() as cursor:
        cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        cursor.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
        assert cursor.fetchone() == (True,), "L5 requires the clone superuser"
        cursor.execute("SET LOCAL row_security = off")
        cursor.execute(
            "SELECT seqrelid FROM pg_sequence "
            "WHERE seqrelid = pg_get_serial_sequence(%s, 'id')::regclass "
            "AND seqincrement = 1 AND seqcache = 1 AND NOT seqcycle",
            (".".join(_RECORDER),),
        )
        recorder_sequence = cursor.fetchone()
        recorder_oid = int(recorder_sequence[0]) if recorder_sequence else None
        if entry is None:
            assert recorder_oid in relations, "expected Django's recorder allocator"
        intact = True
        if entry is not None:
            cursor.execute(_RECORDER_ROW + sql.SQL("id = %s"), (entry[0],))
            intact = cursor.fetchone() == entry
        rows: dict[int, str] = {}
        sequences: dict[int, SequenceState] = {}
        for oid, (schema, name, kind, _) in relations.items():
            relation = sql.Identifier(schema, name)
            if kind == "S":
                cursor.execute(
                    sql.SQL("SELECT last_value, is_called FROM {}").format(relation)
                )
                (state,) = cursor.fetchall()
                sequences[oid] = cast("SequenceState", state)
            elif kind in {"r", "p", "f", "m"}:
                # ONLY avoids counting the same inheritance/partition data
                # under both its parent and its separately observed relation.
                ignore_entry = (schema, name) == _RECORDER and entry is not None
                predicate = (
                    sql.SQL("WHERE item.id <> %s") if ignore_entry else sql.SQL("")
                )
                # ROW(item.*) expands the columns even if one is named item.
                # record_send frames NULL with length -1, not an empty value.
                # Prefix the sorted, duplicate-preserving 32-byte hashes with
                # the row count. Ordering bytea does not use a collation.
                cursor.execute(
                    sql.SQL("""
                        SELECT encode(sha256(
                            int8send(count(*)) || COALESCE(
                                string_agg(h.hash, ''::bytea ORDER BY h.hash), ''::bytea
                            )
                        ), 'hex')
                        FROM (
                            SELECT sha256(pg_catalog.record_send(ROW(item.*))) AS hash
                            FROM ONLY {} AS item {}
                        ) AS h
                    """).format(relation, predicate),
                    (entry[0],) if ignore_entry and entry is not None else (),
                )
                ((digest,),) = cursor.fetchall()
                rows[oid] = digest
    return CommittedData(rows, sequences, recorder_oid, intact)
