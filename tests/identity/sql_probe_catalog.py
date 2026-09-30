"""Live PostgreSQL signatures for the permission census's SQL probes."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from django.db.backends.utils import CursorWrapper

PYTHON_TYPES: dict[str, type[object]] = {
    "pg_catalog.bool": bool,
    "pg_catalog.date": dt.date,
    "pg_catalog.int4": int,
    "pg_catalog.int8": int,
    "pg_catalog.text": str,
    "pg_catalog.timestamptz": dt.datetime,
    "pg_catalog.uuid": UUID,
    "pg_catalog.varchar": str,
}


@dataclass(frozen=True, slots=True)
class Signature:
    returns_set: bool
    boolean_result: bool
    columns: tuple[type[object], ...]
    arguments: tuple[str, ...]
    declared_return: str


_QUERY = """
SELECT p.prokind, p.proretset, p.pronargs, rt.typtype,
       rn.nspname || '.' || rt.typname,
       ARRAY(
           SELECT tn.nspname || '.' || t.typname
             FROM unnest(p.proallargtypes, p.proargmodes)
                  WITH ORDINALITY AS a(type_oid, mode, position)
             JOIN pg_catalog.pg_type t ON t.oid = a.type_oid
             JOIN pg_catalog.pg_namespace tn ON tn.oid = t.typnamespace
            WHERE a.mode IN ('o', 'b', 't')
            ORDER BY a.position
       ),
       ARRAY(
           SELECT tn.nspname || '.' || t.typname
             FROM pg_catalog.pg_attribute a
             JOIN pg_catalog.pg_type t ON t.oid = a.atttypid
             JOIN pg_catalog.pg_namespace tn ON tn.oid = t.typnamespace
            WHERE a.attrelid = rt.typrelid AND a.attnum > 0
              AND NOT a.attisdropped
            ORDER BY a.attnum
       ),
       ARRAY(
           SELECT tn.nspname || '.' || t.typname
             FROM unnest(p.proargtypes) WITH ORDINALITY AS a(type_oid, position)
             JOIN pg_catalog.pg_type t ON t.oid = a.type_oid
             JOIN pg_catalog.pg_namespace tn ON tn.oid = t.typnamespace
            ORDER BY a.position
       ),
       pg_catalog.pg_get_function_result(p.oid)
  FROM pg_catalog.pg_proc p
  JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
  JOIN pg_catalog.pg_type rt ON rt.oid = p.prorettype
  JOIN pg_catalog.pg_namespace rn ON rn.oid = rt.typnamespace
 WHERE n.nspname = %s AND p.proname = %s
"""


def signature(function: str, cursor: CursorWrapper) -> Signature:
    schema, name = function.split(".")
    cursor.execute(_QUERY, [schema, name])
    rows = cursor.fetchall()
    assert len(rows) == 1, (function, "not exactly one pg_proc row", len(rows))
    ((kind, returns_set, arity, type_kind, name, out, attrs, args, result),) = rows
    assert kind == "f", (function, "unknown shape: not a plain function", kind)
    if out:
        names = list(out)
    elif type_kind == "c":
        names = list(attrs)
    elif type_kind == "b":
        names = [name]
    else:
        raise AssertionError((function, "unknown shape", type_kind, name))
    unknown = [column for column in names if column not in PYTHON_TYPES]
    assert names, (function, "unknown shape: no result column")
    assert not unknown, (function, "unknown shape: result column types", unknown)
    unknown_args = [
        argument
        for argument in args
        if argument not in PYTHON_TYPES and argument != "pg_catalog._text"
    ]
    assert not unknown_args, (function, "unknown argument types", unknown_args)
    assert len(args) == arity, (function, "catalog input arity mismatch")
    return Signature(
        returns_set,
        name == "pg_catalog.bool",
        tuple(PYTHON_TYPES[column] for column in names),
        tuple(args),
        result,
    )
