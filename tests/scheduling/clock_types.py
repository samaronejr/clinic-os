"""Live type edges, including domain, array, range and composite containment."""

from __future__ import annotations

from collections import defaultdict
from functools import lru_cache
from typing import TYPE_CHECKING

from django.db import connection
from sqlparse import tokens

from .clock_tokens import sql_tokens

if TYPE_CHECKING:
    from collections.abc import Iterable


class Types:
    def __init__(self, readers: frozenset[int]) -> None:
        self.names: dict[str, set[int]] = defaultdict(set)
        self.links: dict[int, set[int]] = defaultdict(set)
        self.wrappers: dict[int, set[int]] = defaultdict(set)
        self.bases: dict[int, int] = {}
        self.relations: dict[int, set[int]] = defaultdict(set)
        self.functions: dict[int, set[int]] = defaultdict(set)
        self.expressions: dict[int, list[tuple[str, str, str]]] = defaultdict(list)
        self.spellings: dict[str, set[int]] = defaultdict(set)
        self.sql_names: dict[int, str] = {}
        self.row_types: dict[int, int] = {}
        self.field_types: dict[tuple[int, str], int] = {}
        self.prototypes: dict[int, tuple[int, dict[str, int]]] = {}
        self.outputs: dict[int, tuple[int, ...]] = {}
        self.temporal: set[int] = set()
        self.nontext_temporal: set[int] = set()
        self._load_types(readers)
        self._load_fields()
        self._load_functions()
        self._load_domains()
        self._temporal_wrappers()

    def _load_types(self, readers: frozenset[int]) -> None:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT t.oid,t.typname,quote_ident(t.typname),quote_ident(n.nspname),"
                "format_type(t.oid,NULL),t.typbasetype,t.typelem,t.typrelid,"
                "t.typcategory,t.typinput::oid,COALESCE(r.rngsubtype,0) "
                "FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace "
                "LEFT JOIN pg_range r ON t.oid IN (r.rngtypid,r.rngmultitypid)"
            )
            for row in cursor.fetchall():
                (
                    raw_oid,
                    name,
                    quoted,
                    schema,
                    formatted,
                    base,
                    element,
                    relation,
                    category,
                    input_oid,
                    subtype,
                ) = row
                oid = int(raw_oid)
                self.names[str(name)].add(oid)
                if base:
                    self.bases[oid] = int(base)
                self.wrappers[oid].update(int(v) for v in (base, element, subtype) if v)
                self.links[oid].update(self.wrappers[oid])
                for spelling in (quoted, f"{schema}.{quoted}", formatted):
                    self.spellings[self.normalise(str(spelling))].add(oid)
                self.sql_names[oid] = str(formatted)
                if relation:
                    self.row_types[int(relation)] = oid
                if int(input_oid) in readers:
                    self.temporal.add(oid)
                if category in {"D", "T"}:
                    self.nontext_temporal.add(oid)

    def _load_fields(self) -> None:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT a.attrelid,a.atttypid,a.attname,c.relname,n.nspname "
                "FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid "
                "JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE a.attnum>0 AND NOT a.attisdropped"
            )
            for relation, type_oid, field, table, schema in cursor.fetchall():
                self.relations[int(relation)].add(int(type_oid))
                self.field_types[(int(relation), str(field))] = int(type_oid)
                for parts in ((table, field), (schema, table, field)):
                    # These are decoded catalog identifiers, not SQL source.
                    # Re-quoting and lexing every column on every closure build
                    # adds no information; retain their exact case and punctuation.
                    qualified = tuple(
                        value for part in parts for value in (str(part), ".")
                    )[:-1]
                    self.spellings[repr((*qualified, "%", "type"))].add(int(type_oid))
        for relation, oid in self.row_types.items():
            self.links[oid].update(self.relations[relation])

    def _load_functions(self) -> None:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT oid,prorettype,COALESCE(proallargtypes,proargtypes::oid[]),"
                "proargnames,proargmodes FROM pg_proc"
            )
            for oid, result, arguments, names, modes in cursor.fetchall():
                self.outputs[int(oid)] = tuple(
                    int(arg)
                    for arg, mode in zip(
                        arguments, modes or ["i"] * len(arguments), strict=True
                    )
                    if mode in {"o", "b", "t"}
                )
                self.functions[int(oid)].update(map(int, [result, *arguments]))
                self.prototypes[int(oid)] = (
                    int(result),
                    {
                        str(name or f"argument_{index}"): int(type_oid)
                        for index, (name, type_oid) in enumerate(
                            zip(names or [""] * len(arguments), arguments, strict=True),
                            start=1,
                        )
                    },
                )

    def _load_domains(self) -> None:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT oid,'domain-default:' || oid::regtype::text,"
                "pg_get_expr(typdefaultbin,0),typdefaultbin::text FROM pg_type "
                "WHERE typtype='d' AND typdefaultbin IS NOT NULL UNION ALL "
                "SELECT contypid,'domain-constraint:' || contypid::regtype::text "
                "|| '.' || conname,pg_get_constraintdef(oid),conbin::text "
                "FROM pg_constraint WHERE contypid<>0"
            )
            for oid, key, source, tree in cursor.fetchall():
                self.expressions[int(oid)].append((str(key), str(source), str(tree)))

    def _temporal_wrappers(self) -> None:
        changed = True
        while changed:
            changed = False
            for oid, children in self.links.items():
                # Composite containment makes the destination temporal too.
                # It does not prove every source field is non-text temporal.
                for group, members in (
                    (self.temporal, children),
                    (self.nontext_temporal, self.wrappers.get(oid, set())),
                ):
                    if oid not in group and members & group:
                        group.add(oid)
                        changed = True

    @staticmethod
    @lru_cache(maxsize=4096)
    def normalise(value: str) -> str:
        parts = tuple(
            text[1:-1].replace('""', '"')
            if kind in tokens.Literal.String.Symbol
            else "[]"
            if text.startswith("[") and text.endswith("]") and not text[1:-1].strip()
            else text.lower()
            for kind, text in sql_tokens(value)
            if kind not in tokens.Whitespace and kind not in tokens.Comment
        )
        return repr(parts[:-2] if parts[-2:] == ("%", "rowtype") else parts)

    def base(self, oid: int) -> int:
        while oid in self.bases:
            oid = self.bases[oid]
        return oid

    def resolve(self, value: str) -> int:
        found = self.spellings.get(self.normalise(value), set())
        return next(iter(found)) if len(found) == 1 else 0

    def closure(self, roots: Iterable[int]) -> set[int]:
        found: set[int] = set()
        pending = list(roots)
        while pending:
            oid = pending.pop()
            if oid not in found:
                found.add(oid)
                pending.extend(self.links[oid] - found)
        return found

    def surfaces(self, roots: Iterable[int]) -> list[tuple[str, str, str]]:
        return [
            expression
            for oid in sorted(self.closure(roots))
            for expression in self.expressions[oid]
        ]
