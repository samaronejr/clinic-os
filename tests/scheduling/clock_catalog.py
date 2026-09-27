"""Catalog-derived clock readers and transitive scheduling SQL expression census."""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, cast

from django.db import connection
from sqlparse import tokens

from .clock_boundary import sql_boundary_violations
from .clock_dependencies import sql_dependencies
from .clock_temporal_inputs import LiteralInputs
from .clock_tokens import sql_tokens

if TYPE_CHECKING:
    from collections.abc import Iterable

# SQL value expressions are grammar nodes, not pg_proc functions. Unlike the
# function reader set, this is SQL syntax (including precision forms), not a
# sampled vocabulary of clock-reader function names.
SQL_VALUE = re.compile(
    r"\b(?:CURRENT_(?:DATE|TIME|TIMESTAMP)|LOCALTIME(?:STAMP)?)\b"
    r"(?:\s*\(\s*\d+\s*\))?",
    re.IGNORECASE,
)
IDENTIFIER = r'(?:"(?:[^"]|"")+"|[a-zA-Z_][\w$]*)'
CALL = re.compile(
    rf"(?<![\w$])(?:(?P<schema>{IDENTIFIER})\s*\.\s*)?"
    rf"(?P<name>{IDENTIFIER})\s*\("
)
NODE_FUNCTION = re.compile(r":(?:funcid|opfuncid|aggfnoid|winfnoid) (\d+)")
NODE_OPERATOR = re.compile(r":opno (\d+)")
NODE_IO_CAST = re.compile(
    r"\{COERCEVIAIO\b[^{}]*(?:\{[^{}]*\}[^{}]*)*:resulttype (\d+)"
)


class ClockNode(TypedDict):
    direct: dict[str, int]
    via: list[str]


@dataclass(frozen=True)
class Function:
    oid: int
    schema: str
    name: str
    arguments: str
    returns: str
    volatility: str
    source: str
    tree: str
    language: str
    implementation: str
    library: str
    extension: str

    @property
    def identity(self) -> str:
        return f"{self.schema}.{self.name}({self.arguments})"


@dataclass(frozen=True)
class Surface:
    key: str
    source: str
    tree: str = ""
    calls: frozenset[int] = frozenset()
    relations: frozenset[int] = frozenset()
    opaque: str = ""


def functions() -> dict[int, Function]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT p.oid,n.nspname,p.proname,oidvectortypes(p.proargtypes),"
            "p.prorettype::regtype::text,p.provolatile, "
            "CASE WHEN p.prosqlbody IS NOT NULL THEN pg_get_functiondef(p.oid) "
            "ELSE p.prosrc END || ' ' || "
            "COALESCE(pg_get_expr(p.proargdefaults,0),'') AS source, "
            "COALESCE(p.proargdefaults::text,'') || ' ' || "
            "COALESCE(p.prosqlbody::text,'') AS tree, l.lanname,p.prosrc,"
            "COALESCE(p.probin,''),COALESCE((SELECT e.extname FROM pg_depend d "
            "JOIN pg_extension e ON e.oid=d.refobjid "
            "WHERE d.classid='pg_proc'::regclass AND d.objid=p.oid "
            "AND d.refclassid='pg_extension'::regclass AND d.deptype='e'),'') "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
            "JOIN pg_language l ON l.oid=p.prolang"
        )
        return {
            int(row[0]): Function(int(row[0]), *(str(value) for value in row[1:]))
            for row in cursor.fetchall()
        }


def catalog_readers() -> frozenset[int]:
    """Treat the entire non-immutable temporal-returning catalog set as readers.

    timeofday() is the catalog-verified text-returning clock intrinsic. SQL value
    expressions are detected independently; neither is misreported as a member
    of the temporal-return-type query.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT p.oid FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
            "WHERE n.nspname='pg_catalog' AND p.provolatile IN ('s','v') "
            "AND (p.prorettype=ANY(ARRAY['timestamptz'::regtype,'timestamp'::regtype,"
            "'date'::regtype,'time'::regtype,'timetz'::regtype]) "
            "OR p.oid='pg_catalog.timeofday()'::regprocedure)"
        )
        return frozenset(int(row[0]) for row in cursor.fetchall())


def _scheduling_relations() -> list[int]:
    """Include named scheduling relations and dependent views, regardless of name."""
    with connection.cursor() as cursor:
        cursor.execute(
            "WITH RECURSIVE links(parent,child) AS ("
            "SELECT inhparent,inhrelid FROM pg_inherits UNION "
            "SELECT d.refobjid,r.ev_class FROM pg_depend d JOIN pg_rewrite r "
            "ON d.classid='pg_rewrite'::regclass AND r.oid=d.objid "
            "WHERE d.refclassid='pg_class'::regclass), relations(oid) AS ("
            "SELECT c.oid FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE ((n.nspname='clinic_app' AND c.relname LIKE 'scheduling_%%') "
            "OR n.oid=pg_my_temp_schema()) "
            "AND c.relkind IN ('r','p','f','v','m') UNION "
            "SELECT links.child FROM relations JOIN links ON links.parent=relations.oid"
            ") SELECT oid FROM relations ORDER BY oid"
        )
        return [int(row[0]) for row in cursor.fetchall()]


def surfaces(relations: list[int] | None = None) -> list[Surface]:
    """Read every surface of the supplied relations, or the scheduling roots."""
    if relations is None:
        relations = _scheduling_relations()
    queries = (
        # Generated columns share pg_attrdef with ordinary defaults.
        (
            "SELECT CASE WHEN a.attgenerated='' THEN 'default:' ELSE 'generated:' END "
            "|| c.oid::regclass::text || '.' || a.attname, "
            "pg_get_expr(d.adbin,d.adrelid),d.adbin::text "
            "FROM pg_attrdef d JOIN pg_attribute a ON a.attrelid=d.adrelid "
            "AND a.attnum=d.adnum JOIN pg_class c ON c.oid=d.adrelid "
            "WHERE c.oid=ANY(%s)"
        ),
        (
            "SELECT 'policy:' || polrelid::regclass::text || '.' || polname, "
            "COALESCE(pg_get_expr(polqual,polrelid),'') || ' ' || "
            "COALESCE(pg_get_expr(polwithcheck,polrelid),''), "
            "COALESCE(polqual::text,'') || ' ' || COALESCE(polwithcheck::text,'') "
            "FROM pg_policy WHERE polrelid=ANY(%s)"
        ),
        (
            "SELECT 'view:' || oid::regclass::text,pg_get_viewdef(oid),'' "
            "FROM pg_class WHERE oid=ANY(%s) AND relkind IN ('v','m')"
        ),
        (
            "SELECT 'rule:' || ev_class::regclass::text || '.' || rulename, "
            "pg_get_ruledef(oid),ev_qual::text || ' ' || ev_action::text "
            "FROM pg_rewrite WHERE ev_class=ANY(%s)"
        ),
        (
            "SELECT 'index:' || indexrelid::regclass::text, "
            "pg_get_indexdef(indexrelid),COALESCE(indexprs::text,'') || ' ' || "
            "COALESCE(indpred::text,'') FROM pg_index WHERE indrelid=ANY(%s)"
        ),
        (
            "SELECT 'partition:' || partrelid::regclass::text, "
            "pg_get_partkeydef(partrelid),COALESCE(partexprs::text,'') "
            "FROM pg_partitioned_table WHERE partrelid=ANY(%s)"
        ),
    )
    result: list[Surface] = []
    with connection.cursor() as cursor:
        cursor.execute(
            "WITH RECURSIVE types(oid) AS ("
            "SELECT atttypid FROM pg_attribute WHERE attrelid=ANY(%s) "
            "AND attnum>0 AND NOT attisdropped UNION "
            "SELECT t.typbasetype FROM types JOIN pg_type t ON t.oid=types.oid "
            "WHERE t.typbasetype<>0) SELECT 'domain-default:' || t.oid::regtype::text, "
            "pg_get_expr(t.typdefaultbin,0),t.typdefaultbin::text "
            "FROM types JOIN pg_type t ON t.oid=types.oid WHERE t.typtype='d' "
            "AND t.typdefaultbin IS NOT NULL UNION ALL "
            "SELECT 'domain-constraint:' || c.contypid::regtype::text "
            "|| '.' || c.conname, "
            "pg_get_constraintdef(c.oid),COALESCE(c.conbin::text,'') "
            "FROM pg_constraint c JOIN types ON c.contypid=types.oid",
            [relations],
        )
        result.extend(
            Surface(str(key), str(source), str(tree))
            for key, source, tree in cursor.fetchall()
        )
        for query in queries:
            cursor.execute(query, [relations])
            result.extend(
                Surface(str(key), str(source), str(tree))
                for key, source, tree in cursor.fetchall()
            )
        cursor.execute(
            "SELECT 'constraint:' || c.conrelid::regclass::text || '.' || c.conname, "
            "pg_get_constraintdef(c.oid),COALESCE(c.conbin::text,''), "
            "ARRAY(SELECT o.oprcode::oid FROM unnest(c.conexclop) op(oid) "
            "JOIN pg_operator o ON o.oid=op.oid) "
            "FROM pg_constraint c WHERE c.conrelid=ANY(%s)",
            [relations],
        )
        result.extend(
            Surface(str(key), str(source), str(tree), frozenset(map(int, calls)))
            for key, source, tree, calls in cursor.fetchall()
        )
        cursor.execute(
            "SELECT 'trigger:' || tgrelid::regclass::text || '.' || tgname, "
            "pg_get_triggerdef(oid),COALESCE(tgqual::text,''),tgfoid "
            "FROM pg_trigger WHERE tgrelid=ANY(%s) AND NOT tgisinternal",
            [relations],
        )
        result.extend(
            Surface(str(key), str(source), str(tree), frozenset({int(oid)}))
            for key, source, tree, oid in cursor.fetchall()
        )
    return result


def _identifier(value: str) -> str:
    return value[1:-1].replace('""', '"') if value.startswith('"') else value.lower()


def opaque_marker(procedure: Function) -> str:
    """Retain approved opaque risk; never certify native code clock-free."""
    if procedure.language in {"sql", "plpgsql"}:
        return ""
    allowlist = cast(
        "dict[str, dict[str, str]]",
        json.loads(Path(__file__).with_name("clock_opaque_allowlist.json").read_text()),
    )
    entry = allowlist.get(procedure.identity, {})
    approved = bool(entry.get("reason")) and all(
        entry.get(field) == getattr(procedure, field)
        for field in (
            "language",
            "implementation",
            "library",
            "extension",
            "returns",
            "volatility",
        )
    )
    status = "reviewed" if approved else "unreviewed"
    return f"opaque:{status}:{procedure.identity}"


class Census:
    """Conservatively resolve overloaded calls and walk helpers with cycle safety."""

    def __init__(
        self, procedures: dict[int, Function], readers: frozenset[int]
    ) -> None:
        self.procedures = procedures
        self.readers = readers
        self.literal_inputs = LiteralInputs(readers)
        self.relation_surfaces: dict[int, list[Surface]] = {}
        self.by_name: dict[str, set[int]] = defaultdict(set)
        for oid, procedure in procedures.items():
            self.by_name[procedure.name].add(oid)
        with connection.cursor() as cursor:
            cursor.execute("SELECT oid,oprcode::oid FROM pg_operator WHERE oprcode<>0")
            self.operators = {
                int(oid): int(function) for oid, function in cursor.fetchall()
            }
            cursor.execute(
                "SELECT oprname,oprcode::oid FROM pg_operator WHERE oprcode=ANY(%s)",
                [list(readers)],
            )
            self.reader_operators: dict[str, set[int]] = defaultdict(set)
            for name, function in cursor.fetchall():
                self.reader_operators[str(name)].add(int(function))
            cursor.execute("SELECT oid,typinput::oid FROM pg_type WHERE typinput<>0")
            self.inputs = {
                int(oid): int(function) for oid, function in cursor.fetchall()
            }
            cursor.execute(
                "SELECT objid,refobjid FROM pg_depend "
                "WHERE classid='pg_proc'::regclass AND refclassid='pg_proc'::regclass"
            )
            self.dependencies: dict[int, set[int]] = defaultdict(set)
            for caller, callee in cursor.fetchall():
                self.dependencies[int(caller)].add(int(callee))
            cursor.execute(
                "SELECT c.oid,'view:' || c.oid::regclass::text,pg_get_viewdef(c.oid),"
                "r.ev_action::text || ' ' || r.ev_qual::text,r.oid,c.relname "
                "FROM pg_class c JOIN pg_rewrite r ON r.ev_class=c.oid "
                "WHERE c.relkind IN ('v','m') AND r.rulename='_RETURN'"
            )
            view_rows = cursor.fetchall()
            cursor.execute(
                "SELECT objid,refclassid::regclass::text,refobjid FROM pg_depend "
                "WHERE classid='pg_rewrite'::regclass "
                "AND refclassid IN ('pg_proc'::regclass,'pg_class'::regclass)"
            )
            view_functions: dict[int, set[int]] = defaultdict(set)
            view_relations: dict[int, set[int]] = defaultdict(set)
            for rule, kind, reference in cursor.fetchall():
                target = view_functions if kind == "pg_proc" else view_relations
                target[int(rule)].add(int(reference))
            self.views = {
                int(oid): Surface(
                    str(key),
                    str(source),
                    str(tree),
                    frozenset(view_functions[int(rule)]),
                    frozenset(view_relations[int(rule)]),
                )
                for oid, key, source, tree, rule, _ in view_rows
            }
            self.view_names = {str(row[5]) for row in view_rows}

    def unresolved_marker(self, procedure: Function) -> str:
        if procedure.language not in {"sql", "plpgsql"}:
            return opaque_marker(procedure)
        if sql_boundary_violations(procedure.source):
            return f"unresolved-sql-boundary:{procedure.identity}"
        if procedure.language == "plpgsql":
            # PL/pgSQL does not persist relation dependencies. Do not pretend a
            # token match is a catalog edge: a possible view reference is risky.
            identifiers = {
                value.strip('"')
                for kind, value in sql_tokens(procedure.implementation)
                if kind in tokens.Name or kind in tokens.Literal.String.Symbol
            }
            if identifiers & self.view_names:
                return f"unresolved-plpgsql-view:{procedure.identity}"
        return ""

    def function_surface(self, procedure: Function) -> Surface:
        marker = self.unresolved_marker(procedure)
        calls = set(self.dependencies[procedure.oid])
        relations: frozenset[int] = frozenset()
        if procedure.language == "plpgsql":
            # Attribute notation and typed declarations need no name(...).
            # Resolve every identifier against live pg_proc, including overloads.
            calls.update(
                oid
                for kind, value in sql_tokens(procedure.implementation)
                if kind in tokens.Name or kind in tokens.Literal.String.Symbol
                for oid in self.by_name.get(_identifier(value), ())
            )
        if procedure.language == "sql":
            dependencies = sql_dependencies(procedure.oid)
            calls.update(dependencies.functions)
            relations = dependencies.relations
            if dependencies.unresolved:
                marker = f"unresolved-sql:{procedure.identity}"
        return Surface(
            "function:" + procedure.identity,
            procedure.source,
            procedure.tree,
            frozenset(calls),
            relations,
            marker,
        )

    def operator_calls(self, source: str) -> Counter[int]:
        return Counter(
            oid
            for token, value in sql_tokens(source)
            if token in tokens.Operator
            for oid in self.reader_operators.get(value, ())
        )

    def related_surfaces(self, oid: int) -> list[Surface]:
        if oid not in self.relation_surfaces:
            self.relation_surfaces[oid] = (
                [self.views[oid]] if oid in self.views else surfaces([oid])
            )
        return self.relation_surfaces[oid]

    def inspect(self, surface: Surface) -> tuple[Counter[str], set[int]]:
        calls = set(surface.calls)
        textual: Counter[str] = Counter()
        if surface.opaque:
            textual[surface.opaque] = 1
            if surface.opaque.startswith("opaque:"):
                return textual, calls
        textual.update(
            {"unresolved-temporal-input": 1}
            if self.literal_inputs.unresolved(surface.source)
            else {}
        )
        for match in CALL.finditer(surface.source):
            schema = match.group("schema")
            for oid in self.by_name.get(_identifier(match.group("name")), ()):
                procedure = self.procedures[oid]
                if schema is not None and procedure.schema != _identifier(schema):
                    continue
                calls.add(oid)
                if oid in self.readers:
                    textual[procedure.identity] += 1
        operators = self.operator_calls(surface.source)
        calls.update(operators)
        textual.update(
            {self.procedures[oid].identity: count for oid, count in operators.items()}
        )
        for match in SQL_VALUE.finditer(surface.source):
            textual["sql:" + re.sub(r"\s+", "", match.group()).lower()] += 1
        compiled: Counter[str] = Counter(
            self.procedures[oid].identity
            for oid in surface.calls
            if oid in self.readers
        )
        node_calls = [
            int(oid) for oid in NODE_FUNCTION.findall(surface.tree) if int(oid)
        ]
        node_calls.extend(
            self.operators[int(oid)]
            for oid in NODE_OPERATOR.findall(surface.tree)
            if int(oid) in self.operators
        )
        node_calls.extend(
            self.inputs[int(oid)]
            for oid in NODE_IO_CAST.findall(surface.tree)
            if int(oid) in self.inputs
        )
        for oid in node_calls:
            calls.add(oid)
            if oid in self.readers:
                compiled[self.procedures[oid].identity] += 1
        # Same expression has textual and compiled representations: do not count
        # it twice. Compiled nodes also expose operators and casts with no call text.
        return textual | compiled, calls

    def inventory(self, roots: Iterable[Surface]) -> dict[str, ClockNode]:
        direct: dict[str, Counter[str]] = {}
        edges: dict[str, set[str]] = {}
        pending = list(roots)
        while pending:
            surface = pending.pop()
            if surface.key in direct:
                continue
            direct[surface.key], calls = self.inspect(surface)
            edges[surface.key] = set()
            for oid in surface.relations:
                for related in self.related_surfaces(oid):
                    edges[surface.key].add(related.key)
                    pending.append(related)
            for oid in calls:
                procedure = self.procedures.get(oid)
                if procedure is None or oid in self.readers:
                    continue
                key = "function:" + procedure.identity
                edges[surface.key].add(key)
                if key not in direct:
                    pending.append(self.function_surface(procedure))
        clocked = {key for key, counts in direct.items() if counts}
        while True:
            inherited = {key for key, callees in edges.items() if callees & clocked}
            expanded = clocked | inherited
            if expanded == clocked:
                break
            clocked = expanded
        return {
            key: {
                "direct": dict(sorted(direct[key].items())),
                "via": sorted(edges[key] & clocked),
            }
            for key in sorted(clocked)
        }


def live_clock_inventory() -> dict[str, ClockNode]:
    procedures = functions()
    census = Census(procedures, catalog_readers())
    roots = surfaces()
    with connection.cursor() as cursor:
        cursor.execute("SELECT name,statement,from_sql FROM pg_prepared_statements")
        roots.extend(
            Surface(
                "prepared:" + str(name),
                str(statement),
                opaque="unresolved-prepared" if from_sql else "",
            )
            for name, statement, from_sql in cursor.fetchall()
        )
        cursor.execute("SELECT pg_my_temp_schema()")
        temporary = cursor.fetchone()
        assert temporary is not None
        temporary_schema = int(temporary[0])
        cursor.execute(
            "SELECT nspname FROM pg_namespace WHERE oid=%s", [temporary_schema]
        )
        temporary_name = cursor.fetchone()
    roots.extend(
        census.function_surface(function)
        for function in procedures.values()
        if (
            function.schema == "clinic_app"
            and function.name.startswith(
                ("scheduling_", "patient_booking_", "waitlist_")
            )
        )
        or (temporary_name is not None and function.schema == temporary_name[0])
    )
    return census.inventory(roots)
