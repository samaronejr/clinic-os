"""Read PostgreSQL's binary value for every live application-table column."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from psycopg import sql

if TYPE_CHECKING:
    from collections.abc import Collection

    import psycopg


@dataclass(frozen=True)
class Column:
    """Physical column metadata used to select typed sensitivity checks."""

    name: str
    data_type: str
    is_fk: bool


@dataclass(frozen=True)
class ReadFault:
    """Flip the last payload bit of one selected synthetic projection cell."""

    key: str
    column: str


type Cells = dict[str, tuple[bytes, ...]]


class BinaryProjectionMismatchError(ValueError):
    """The binary row read disagreed with its pristine snapshot."""


def verify_projection(table: str, expected: Cells, actual: Cells) -> None:
    """Require the same primary keys and exact framed bytes for every cell."""
    if actual != expected:
        message = f"byte_preserved:{table}"
        raise BinaryProjectionMismatchError(message)


def read_projection(
    conn: psycopg.Connection,
    table: str,
    historical_columns: Collection[str],
    *,
    fault: ReadFault | None = None,
) -> tuple[list[Column], Cells]:
    """Use record_send for length, type, NULL and binary payload framing."""
    catalog = conn.execute(
        "SELECT a.attname, t.typname, EXISTS (SELECT 1 FROM pg_constraint fk "
        "WHERE fk.conrelid=c.oid AND fk.contype='f' AND a.attnum=ANY(fk.conkey)), "
        "t.typsend<>0 FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid "
        "JOIN pg_namespace n ON n.oid=c.relnamespace "
        "JOIN pg_type t ON t.oid=a.atttypid WHERE n.nspname='clinic_app' "
        "AND c.relname=%s AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attnum",
        (table,),
    ).fetchall()
    columns = [Column(name, kind, fk) for name, kind, fk, _ in catalog]
    missing = set(historical_columns) - {column.name for column in columns}
    unsupported = [name for name, _, _, supported in catalog if not supported]
    if not columns or missing or unsupported:
        message = (
            f"binary_projection_unavailable:{table}:{sorted(missing)}:{unsupported}"
        )
        raise ValueError(message)
    if fault is not None and fault.column not in {column.name for column in columns}:
        message = f"binary_fault_column_missing:{table}.{fault.column}"
        raise ValueError(message)
    fields = sql.SQL(",").join(_field(column.name, fault) for column in columns)
    result = conn.execute(
        sql.SQL("SELECT t.id::text,{} FROM {} t ORDER BY t.id").format(
            fields, sql.Identifier("clinic_app", table)
        )
    )
    return columns, {row[0]: tuple(bytes(cell) for cell in row[1:]) for row in result}


def _field(name: str, fault: ReadFault | None) -> sql.Composable:
    frame = sql.SQL("pg_catalog.record_send(ROW(t.{}))").format(sql.Identifier(name))
    if fault is None or fault.column != name:
        return frame
    return sql.SQL(
        "CASE WHEN t.id::text={key} AND octet_length({frame})>12 "
        "THEN set_byte({frame}, octet_length({frame})-1, "
        "get_byte({frame}, octet_length({frame})-1) # 1) ELSE {frame} END"
    ).format(key=sql.Literal(fault.key), frame=frame)
