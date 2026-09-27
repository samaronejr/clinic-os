"""Session-shared closure cache invalidated by complete semantic catalog state."""

from __future__ import annotations

import copy
import json
from collections import OrderedDict
from pathlib import Path
from typing import TYPE_CHECKING

from django.db import connection

from .clock_catalog_rows import PHYSICAL_COLUMNS
from .clock_datetime_tokens import special_datetime_tokens
from .clock_support import SQL_CLOCK_DEFINITIONS

if TYPE_CHECKING:
    from collections.abc import Callable

    from .clock_catalog import ClockNode

_CACHE: OrderedDict[tuple[str, str, bytes, str], dict[str, ClockNode]] = OrderedDict()
CACHE_STATS = {"builds": 0, "hits": 0}

# Hash all rows/edges used by the classifier, not a sample. Physical storage and
# ANALYZE counters are not SQL expression semantics and change during test flush.
# Uncontrolled procedures need no record reconstruction. Canonical byte ordering
# avoids locale comparisons of long expression trees without omitting any row.
CATALOG_VERSION = """
WITH ns AS (
 SELECT oid FROM pg_namespace
 WHERE nspname !~ '^pg_(temp|toast_temp)_' OR oid=pg_my_temp_schema()
), overrides AS (
 SELECT key::regprocedure::oid AS oid,value FROM jsonb_each(%s::jsonb)
), rows(kind, value) AS (
 SELECT 'proc',CASE
 WHEN o.oid IS NOT NULL AND pg_get_functiondef(p.oid)=o.value->>'expected'
 THEN jsonb_populate_record(p,jsonb_build_object('prosrc',o.value->>'shape'))::text
 ELSE ROW(p.*)::text END
 FROM pg_proc p JOIN ns ON ns.oid=p.pronamespace LEFT JOIN overrides o ON o.oid=p.oid
 UNION ALL SELECT 'class',ROW(whole.*)::text
 FROM pg_class c JOIN ns ON ns.oid=c.relnamespace
 CROSS JOIN LATERAL jsonb_populate_record(c,'__CLASS_PHYSICAL__'::jsonb) whole
 UNION ALL SELECT 'attribute',ROW(whole.*)::text FROM pg_attribute a
 JOIN pg_class c ON c.oid=a.attrelid JOIN ns ON ns.oid=c.relnamespace
 CROSS JOIN LATERAL jsonb_populate_record(a,'__ATTRIBUTE_PHYSICAL__'::jsonb ||
 jsonb_build_object('attacl',NULLIF(a.attacl,'{}'::aclitem[]))) whole
 WHERE a.attnum>0 AND NOT a.attisdropped
 UNION ALL SELECT 'default-acl',ROW(x.*)::text FROM pg_default_acl x
 UNION ALL SELECT 'sequence',ROW(x.*)::text FROM pg_sequence x
 UNION ALL SELECT 'index',ROW(x.*)::text FROM pg_index x
 UNION ALL SELECT 'default',ROW(x.*)::text FROM pg_attrdef x
 UNION ALL SELECT 'constraint',ROW(x.*)::text FROM pg_constraint x
 UNION ALL SELECT 'policy',ROW(x.*)::text FROM pg_policy x
 UNION ALL SELECT 'rewrite',ROW(x.*)::text FROM pg_rewrite x
 UNION ALL SELECT 'trigger',ROW(x.*)::text FROM pg_trigger x
 UNION ALL SELECT 'event-trigger',ROW(x.*)::text FROM pg_event_trigger x
 UNION ALL SELECT 'type',ROW(x.*)::text FROM pg_type x JOIN ns ON ns.oid=x.typnamespace
 UNION ALL SELECT 'range',ROW(x.*)::text FROM pg_range x
 UNION ALL SELECT 'namespace',ROW(x.*)::text FROM pg_namespace x JOIN ns ON ns.oid=x.oid
 UNION ALL SELECT 'dependency',ROW(x.*)::text FROM pg_depend x
 UNION ALL SELECT 'operator',ROW(x.*)::text FROM pg_operator x
 UNION ALL SELECT 'cast',ROW(x.*)::text FROM pg_cast x
 UNION ALL SELECT 'inheritance',ROW(x.*)::text FROM pg_inherits x
 UNION ALL SELECT 'partition',ROW(x.*)::text FROM pg_partitioned_table x
 UNION ALL SELECT 'extension',ROW(x.*)::text FROM pg_extension x
 UNION ALL SELECT 'language',ROW(x.*)::text FROM pg_language x
 UNION ALL SELECT 'database-environment',
 ROW(d.datdba,d.datacl,d.datcollate,d.datctype)::text
 FROM pg_database d WHERE d.datname=current_database()
 UNION ALL SELECT 'database-settings',ROW(s.setrole,s.setconfig)::text
 FROM pg_db_role_setting s JOIN pg_database d ON d.oid=s.setdatabase
 WHERE d.datname=current_database()
 UNION ALL SELECT 'prepared',ROW(x.*)::text FROM pg_prepared_statements x
 UNION ALL SELECT 'session',ROW(current_user,current_role,session_user,
 current_setting('search_path'),
 current_setting('standard_conforming_strings'),current_setting('TimeZone'),
 current_setting('DateStyle'),pg_my_temp_schema())::text
)
SELECT sha256(convert_to(
 string_agg(kind||':'||value,E'\\n'
 ORDER BY kind COLLATE "C",value COLLATE "C"),'UTF8'))
FROM rows
""".replace(
    "__CLASS_PHYSICAL__", json.dumps(dict.fromkeys(PHYSICAL_COLUMNS["pg_class"]))
).replace(
    "__ATTRIBUTE_PHYSICAL__",
    json.dumps(dict.fromkeys(PHYSICAL_COLUMNS["pg_attribute"])),
)


def catalog_version() -> bytes:
    with connection.cursor() as cursor:
        cursor.execute(CATALOG_VERSION, [json.dumps(SQL_CLOCK_DEFINITIONS.get() or {})])
        row = cursor.fetchone()
    assert row is not None
    return bytes(row[0])


def shared_inventory(
    build: Callable[[], dict[str, ClockNode]], opaque_policy: dict[str, dict[str, str]]
) -> dict[str, ClockNode]:
    policy = json.dumps(
        [
            opaque_policy,
            sorted(special_datetime_tokens()),
            Path(__file__).with_name("clock_literal_allowlist.json").read_text(),
        ],
        sort_keys=True,
    )
    key = (
        str(connection.settings_dict["HOST"]),
        str(connection.settings_dict["PORT"]),
        catalog_version(),
        policy,
    )
    if key not in _CACHE:
        _CACHE[key] = build()
        CACHE_STATS["builds"] += 1
        if len(_CACHE) > 16:
            _CACHE.popitem(last=False)
    else:
        CACHE_STATS["hits"] += 1
    _CACHE.move_to_end(key)
    return copy.deepcopy(_CACHE[key])


def clear_closure_cache() -> None:
    _CACHE.clear()
