"""Observe relation writes across one protected operation in a clean database."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from django.db.backends.base.base import BaseDatabaseWrapper

type LocalRows = dict[str, set[tuple[str, str]]]

# Row identities matter *within* a capture: dropping and recreating an RI
# trigger is a write on tgrelid even when its normalized definition is equal.
# Never persist these identities or compare them across clean installations.
# Map an index's pg_class/pg_attribute rows to its owning table as well.
_QUERY = """
WITH members AS MATERIALIZED (
    SELECT c.oid, coalesce(t.relname, c.relname) AS owner
    FROM pg_catalog.pg_class c
    LEFT JOIN pg_catalog.pg_index i ON i.indexrelid=c.oid
    LEFT JOIN pg_catalog.pg_class t ON t.oid=i.indrelid
    WHERE c.relnamespace='clinic_app'::regnamespace
)
SELECT m.owner, 'pg_class', (to_jsonb(c) - ARRAY[
    'relpages','reltuples','relallvisible','relfrozenxid','relminmxid',
    'relhasindex','relhassubclass','relhastriggers','relhasrules'])::text
FROM pg_catalog.pg_class c JOIN members m ON m.oid=c.oid
UNION ALL SELECT m.owner, 'pg_attribute', a::text
    FROM pg_catalog.pg_attribute a JOIN members m ON m.oid=a.attrelid
UNION ALL SELECT m.owner, 'pg_attrdef', d::text
    FROM pg_catalog.pg_attrdef d JOIN members m ON m.oid=d.adrelid
UNION ALL SELECT m.owner, 'pg_constraint', k::text
    FROM pg_catalog.pg_constraint k JOIN members m ON m.oid=k.conrelid
UNION ALL SELECT m.owner, 'pg_trigger', g::text
    FROM pg_catalog.pg_trigger g JOIN members m ON m.oid=g.tgrelid
UNION ALL SELECT m.owner, 'pg_index', (to_jsonb(i)-'indcheckxmin')::text
    FROM pg_catalog.pg_index i JOIN members m ON m.oid=i.indrelid
"""


def local_rows(db: BaseDatabaseWrapper) -> LocalRows:
    with db.cursor() as cur:
        cur.execute(_QUERY)
        result: LocalRows = {}
        for owner, catalog, row in cur.fetchall():
            result.setdefault("relation " + owner, set()).add((catalog, row))
        return result


def changed_relations(before: LocalRows, after: LocalRows) -> set[str]:
    return {
        name
        for name in before.keys() | after.keys()
        if before.get(name) != after.get(name)
    }
