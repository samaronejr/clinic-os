"""Live function/relation/view closure of the authoritative staff gates."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from django.db import connection

from identity.authority_sql import references

if TYPE_CHECKING:
    from collections.abc import Iterable

    from identity.authority_sql import SqlReferences

type Node = tuple[str, int, bool]

ROOTS = frozenset({"clinic_app.has_permission", "clinic_app.user_has_org"})
# Native primitives whose contracts cannot execute caller SQL or inspect staff
# rows. Immutable built-in pg_catalog primitives are also accepted. User-created
# native functions never inherit this exception by name or volatility.
CONTEXT_PRIMITIVES = frozenset(
    {
        "current_setting",
        "set_config",
        "now",
        "statement_timestamp",
        "transaction_timestamp",
        "clock_timestamp",
        "timestamptz_pl_interval",
        "timestamptz_mi_interval",
        "txid_current",
        "pg_current_xact_id",
        "pg_advisory_xact_lock",
        "pg_advisory_lock",
        "pg_advisory_unlock",
        "pg_try_advisory_lock",
        "gen_random_uuid",
        "jsonb_build_object",
        "jsonb_build_array",
        "to_jsonb",
        "row_to_json",
        "format",
    }
)
# Cross-type temporal comparisons are STABLE because of TimeZone, not because
# they query application data. Names still require a built-in pg_catalog OID.
TEMPORAL_COMPARISONS = frozenset(
    f"{left}_{comparison}_{right}"
    for left in ("date", "timestamp", "timestamptz")
    for right in ("date", "timestamp", "timestamptz")
    for comparison in ("eq", "ne", "lt", "le", "gt", "ge")
)
# These pgcrypto functions operate only on supplied bytes/keys or fresh entropy.
# Extension membership is checked; a same-named application C function is opaque.
CRYPTO_PRIMITIVES = {
    "digest": "pg_digest",
    "hmac": "pg_hmac",
    "encrypt": "pg_encrypt",
    "decrypt": "pg_decrypt",
    "encrypt_iv": "pg_encrypt_iv",
    "decrypt_iv": "pg_decrypt_iv",
    "gen_random_bytes": "pg_random_bytes",
    "gen_random_uuid": "pg_random_uuid",
    "pgp_sym_encrypt": "pgp_sym_encrypt_text",
    "pgp_sym_decrypt": "pgp_sym_decrypt_text",
    "pgp_sym_encrypt_bytea": "pgp_sym_encrypt_bytea",
    "pgp_sym_decrypt_bytea": "pgp_sym_decrypt_bytea",
}


UNRESOLVED_WRITE = "unresolved write target"
# Catalog relations that list server settings (and so every session GUC).
SETTING_CATALOGS = frozenset(
    {"pg_settings", "pg_file_settings", "pg_db_role_setting", "pg_show_all_settings"}
)


@dataclass(frozen=True)
class Function:
    oid: int
    name: str
    language: str
    volatility: str
    security_definer: bool
    bypass_rls: bool
    source: str
    definition: str
    extension: str | None
    library: str | None
    native_types: bool
    defaults: str | None


@dataclass(frozen=True)
class Relation:
    oid: int
    name: str
    kind: str
    columns: frozenset[str]
    opaque_types: frozenset[str]


@dataclass
class Reads:
    relations: set[int] = field(default_factory=set)
    functions: set[int] = field(default_factory=set)
    settings: set[str] = field(default_factory=set)
    opaque: set[str] = field(default_factory=set)
    # Relations named by a writing statement (INSERT, UPDATE, DELETE, MERGE,
    # TRUNCATE, COPY, SELECT ... FOR UPDATE); an unresolved target is kept
    # as a marker so a write never disappears.
    writes: set[str] = field(default_factory=set)

    def merge(self, other: Reads) -> None:
        self.relations.update(other.relations)
        self.functions.update(other.functions)
        self.settings.update(other.settings)
        self.opaque.update(other.opaque)
        self.writes.update(other.writes)


@dataclass(frozen=True)
class Channels:
    relations: frozenset[int]
    settings: frozenset[str]
    functions: frozenset[int]


def _sql_body(definition: str) -> str | None:
    if "BEGIN ATOMIC" in definition:
        return definition.split("BEGIN ATOMIC", 1)[1].rsplit("END", 1)[0]
    if "\nRETURN " in definition:
        return "SELECT " + definition.split("\nRETURN ", 1)[1]
    return None


class Catalog:
    """Snapshot live definitions, dependencies, rewrite rules and RLS expressions."""

    def __init__(self) -> None:
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT p.oid, n.nspname||'.'||p.proname, l.lanname,
                       p.provolatile, p.prosecdef, r.rolsuper OR r.rolbypassrls,
                       p.prosrc,
                       CASE WHEN p.prokind='f' AND
                            (n.nspname <> 'pg_catalog'
                             OR l.lanname IN ('sql','plpgsql'))
                            THEN pg_get_functiondef(p.oid) ELSE '' END,
                       e.extname, p.probin,
                       NOT EXISTS (
                         SELECT 1 FROM unnest(COALESCE(p.proallargtypes,
                           p.proargtypes::oid[]) || ARRAY[p.prorettype]) AS arg(oid)
                         JOIN pg_type t ON t.oid=arg.oid
                         JOIN pg_namespace tn ON tn.oid=t.typnamespace
                         WHERE tn.nspname<>'pg_catalog'
                       ),
                       pg_get_expr(p.proargdefaults, 0)
                FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
                JOIN pg_language l ON l.oid=p.prolang
                JOIN pg_roles r ON r.oid=p.proowner
                LEFT JOIN pg_depend d ON d.classid='pg_proc'::regclass
                    AND d.objid=p.oid AND d.refclassid='pg_extension'::regclass
                    AND d.deptype='e'
                LEFT JOIN pg_extension e ON e.oid=d.refobjid
            """)
            self.functions = {row[0]: Function(*row) for row in cursor.fetchall()}
            cursor.execute("""
                SELECT c.oid, n.nspname||'.'||c.relname, c.relkind,
                       ARRAY(SELECT a.attname FROM pg_attribute a
                             WHERE a.attrelid=c.oid AND a.attnum>0
                             AND NOT a.attisdropped ORDER BY a.attnum),
                       ARRAY(SELECT tn.nspname||'.'||t.typname FROM pg_attribute a
                             JOIN pg_type t ON t.oid=a.atttypid
                             JOIN pg_namespace tn ON tn.oid=t.typnamespace
                             WHERE a.attrelid=c.oid AND a.attnum>0
                             AND NOT a.attisdropped AND tn.nspname<>'pg_catalog')
                FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE c.relkind IN ('r','p','v','m','f')
                  AND n.nspname NOT IN ('pg_catalog','information_schema')
                  AND n.nspname NOT LIKE 'pg_toast%'
            """)
            self.relations = {
                row[0]: Relation(
                    row[0], row[1], row[2], frozenset(row[3]), frozenset(row[4])
                )
                for row in cursor.fetchall()
            }
            # pg_rewrite dependencies carry a view's relations and functions but
            # not the setting names it reads, so view text is parsed as well.
            cursor.execute("""
                SELECT c.oid, pg_get_viewdef(c.oid) FROM pg_class c
                JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE c.relkind IN ('v','m')
                  AND n.nspname NOT IN ('pg_catalog','information_schema')
                  AND n.nspname NOT LIKE 'pg_toast%'
            """)
            self.view_definitions: dict[int, str] = dict(cursor.fetchall())
            cursor.execute("""
                SELECT objid, refclassid::regclass::text, refobjid FROM pg_depend
                WHERE classid='pg_proc'::regclass
                  AND refclassid IN ('pg_class'::regclass,'pg_proc'::regclass)
            """)
            self.function_edges = cursor.fetchall()
            cursor.execute("""
                SELECT n.nspname, t.typname FROM pg_type t
                JOIN pg_namespace n ON n.oid=t.typnamespace
                LEFT JOIN pg_class c ON c.oid=t.typrelid
                WHERE n.nspname NOT IN ('pg_catalog','information_schema')
                  AND (c.oid IS NULL OR c.relkind='c')
            """)
            self.opaque_types = {tuple(row) for row in cursor.fetchall()}
            self.opaque_types |= {
                ("pg_temp", name)
                for schema, name in self.opaque_types
                if schema.startswith("pg_temp_")
            }
            self.opaque_types |= {(name,) for _schema, name in self.opaque_types}
            cursor.execute("""
                SELECT r.ev_class, d.refclassid::regclass::text, d.refobjid
                FROM pg_rewrite r JOIN pg_depend d
                  ON d.classid='pg_rewrite'::regclass AND d.objid=r.oid
                WHERE d.refclassid IN ('pg_class'::regclass,'pg_proc'::regclass)
            """)
            self.view_edges = cursor.fetchall()
            cursor.execute("""
                SELECT polrelid, pg_get_expr(polqual,polrelid),
                       pg_get_expr(polwithcheck,polrelid) FROM pg_policy
            """)
            self.policies = cursor.fetchall()
            cursor.execute("SELECT oprname, oprcode::oid FROM pg_operator")
            self.operators: dict[str, set[int]] = {}
            for name, oid in cursor.fetchall():
                self.operators.setdefault(name, set()).add(oid)
            cursor.execute(
                "SELECT tgrelid, tgfoid FROM pg_trigger WHERE NOT tgisinternal"
            )
            self.triggers = cursor.fetchall()
            cursor.execute("""
                SELECT adrelid, pg_get_expr(adbin, adrelid) FROM pg_attrdef
                UNION ALL
                SELECT conrelid, pg_get_expr(conbin, conrelid) FROM pg_constraint
                WHERE conbin IS NOT NULL AND conrelid <> 0
                UNION ALL
                SELECT indrelid, concat_ws(' ', pg_get_expr(indexprs, indrelid),
                    pg_get_expr(indpred, indrelid)) FROM pg_index
                WHERE indexprs IS NOT NULL OR indpred IS NOT NULL
            """)
            self.write_expressions = cursor.fetchall()
            cursor.execute("""
                WITH objects AS (
                    SELECT 'pg_attrdef'::regclass AS classid, oid, adrelid AS relid
                    FROM pg_attrdef
                    UNION ALL
                    SELECT 'pg_constraint'::regclass, oid, conrelid
                    FROM pg_constraint WHERE conrelid <> 0
                    UNION ALL
                    SELECT 'pg_class'::regclass, indexrelid, indrelid FROM pg_index
                )
                SELECT o.relid, d.refclassid::regclass::text, d.refobjid
                FROM objects o JOIN pg_depend d
                  ON d.classid=o.classid AND d.objid=o.oid
                WHERE d.refclassid IN ('pg_class'::regclass,'pg_proc'::regclass)
            """)
            self.write_edges = cursor.fetchall()
        self.function_names = self._names(self.functions.values())
        self.relation_names = self._names(self.relations.values())

    @staticmethod
    def _names(
        objects: Iterable[Function | Relation],
    ) -> dict[tuple[str, ...], set[int]]:
        result: dict[tuple[str, ...], set[int]] = {}
        for obj in objects:
            schema, name = obj.name.split(".", 1)
            keys = [(schema, name), (name,)]
            if schema.startswith("pg_temp_"):
                keys.append(("pg_temp", name))
            for key in keys:
                result.setdefault(key, set()).add(obj.oid)
        return result

    def statement(self, text: str, *, bypass: bool = False) -> Reads:
        return self._statement(text, bypass=bypass, seen=set())

    def _statement(self, text: str, *, bypass: bool, seen: set[Node]) -> Reads:
        parsed = references(text)
        result = Reads(settings=parsed.settings, opaque=parsed.opaque)
        functions = self._function_references(parsed, result)
        relations = set()
        for name in parsed.names:
            if name[-1] in SETTING_CATALOGS:
                # Server settings enumerated as rows expose every session GUC.
                result.opaque.add("setting enumeration " + name[-1])
            if name in self.opaque_types:
                result.opaque.add("uninspectable type " + ".".join(name))
            relations.update(self.relation_names.get(name, set()))
            if len(name) > 2:
                relations.update(self.relation_names.get(name[:2], set()))
        for oid in relations:
            result.merge(self._relation(oid, bypass=bypass, seen=seen))
        if parsed.writes:
            result.writes.update(
                {self.relations[oid].name for oid in relations} or {UNRESOLVED_WRITE}
            )
            for oid in tuple(result.relations):
                result.merge(self._write(oid, bypass=bypass, seen=seen))
        for oid in functions:
            result.merge(self._function(oid, bypass=bypass, seen=seen))
        return result

    def _function_references(self, parsed: SqlReferences, result: Reads) -> set[int]:
        functions = set()
        for name in parsed.calls:
            matches = self.function_names.get(name, set())
            if not matches and name not in self.relation_names:
                result.opaque.add("unresolved function " + ".".join(name))
            functions.update(matches)
        for operator in parsed.operators:
            matches = self.operators.get(operator, set())
            if not matches:
                result.opaque.add("unresolved operator " + operator)
            if any(
                self.functions[oid].name == "pg_catalog.current_setting"
                for oid in matches
            ):
                result.opaque.add("indirect setting reader")
            functions.update(matches)
        return functions

    def _relation(self, oid: int, *, bypass: bool, seen: set[Node]) -> Reads:
        node = ("relation", oid, bypass)
        if node in seen:
            return Reads()
        seen.add(node)
        result = Reads(relations={oid})
        relation = self.relations[oid]
        result.opaque.update(
            "uninspectable column type " + name for name in relation.opaque_types
        )
        if relation.kind == "f":
            result.opaque.add("foreign relation " + relation.name)
        for parent, kind, target in self.view_edges:
            if parent == oid:
                result.merge(self._edge(kind, target, bypass=bypass, seen=seen))
        definition = self.view_definitions.get(oid)
        if definition is not None:
            result.merge(self._statement(definition, bypass=bypass, seen=seen))
        if not bypass:
            for table, using, check in self.policies:
                if table == oid:
                    result.merge(
                        self._statement(
                            (using or "") + " " + (check or ""),
                            bypass=bypass,
                            seen=seen,
                        )
                    )
        return result

    def _write(self, oid: int, *, bypass: bool, seen: set[Node]) -> Reads:
        node = ("write", oid, bypass)
        if node in seen:
            return Reads()
        seen.add(node)
        result = Reads()
        for table, expression in self.write_expressions:
            if table == oid:
                result.merge(self._statement(expression, bypass=bypass, seen=seen))
        for table, kind, target in self.write_edges:
            if table == oid:
                result.merge(self._edge(kind, target, bypass=bypass, seen=seen))
        for table, trigger in self.triggers:
            if table == oid:
                result.merge(self._function(trigger, bypass=bypass, seen=seen))
        return result

    def _edge(self, kind: str, oid: int, *, bypass: bool, seen: set[Node]) -> Reads:
        if kind == "pg_proc" and oid in self.functions:
            return self._function(oid, bypass=bypass, seen=seen)
        if kind == "pg_class" and oid in self.relations:
            return self._relation(oid, bypass=bypass, seen=seen)
        return Reads()

    def _defaults(self, function: Function, *, bypass: bool, seen: set[Node]) -> Reads:
        # Parameter defaults are evaluated in the caller's query, so their
        # reads count with the caller's RLS context, not the definer's.
        if not function.defaults:
            return Reads()
        return self._statement("SELECT " + function.defaults, bypass=bypass, seen=seen)

    def _function(self, oid: int, *, bypass: bool, seen: set[Node]) -> Reads:
        function = self.functions[oid]
        caller_bypass = bypass
        bypass = function.bypass_rls if function.security_definer else bypass
        node = ("function", oid, bypass)
        if node in seen:
            return Reads()
        seen.add(node)
        result = Reads(functions={oid})
        result.merge(self._defaults(function, bypass=caller_bypass, seen=seen))
        if not function.native_types:
            result.opaque.add("uninspectable function signature " + function.name)
        if function.language in {"c", "internal"}:
            builtin = function.name.startswith("pg_catalog.") and oid < 16384
            name = function.name.split(".", 1)[1]
            safe = builtin and (
                function.volatility == "i"
                or name in CONTEXT_PRIMITIVES | TEMPORAL_COMPARISONS
            )
            safe |= (
                function.extension == "pgcrypto"
                and function.library == "$libdir/pgcrypto"
                and CRYPTO_PRIMITIVES.get(name) == function.source
            )
            if not safe:
                result.opaque.add("opaque function " + function.name)
            return result
        if function.language not in {"sql", "plpgsql"}:
            result.opaque.add("uninspectable language " + function.name)
            return result
        body: str | None = function.source
        if not body:
            body = _sql_body(function.definition)
            if body is None:
                result.opaque.add("missing function body " + function.name)
                return result
        result.merge(self._statement(body, bypass=bypass, seen=seen))
        for parent, kind, target in self.function_edges:
            if parent == oid:
                result.merge(self._edge(kind, target, bypass=bypass, seen=seen))
        return result

    def derive(self) -> Channels:
        roots = {f.oid for f in self.functions.values() if f.name in ROOTS}
        assert {self.functions[oid].name for oid in roots} == ROOTS
        combined = Reads()
        for oid in roots:
            combined.merge(self._function(oid, bypass=False, seen=set()))
        assert not combined.opaque, ("uninspectable authority gate", combined.opaque)
        return Channels(
            frozenset(combined.relations),
            frozenset(combined.settings),
            frozenset(
                roots
                | {
                    oid
                    for oid in combined.functions
                    if self.functions[oid].security_definer
                }
            ),
        )
