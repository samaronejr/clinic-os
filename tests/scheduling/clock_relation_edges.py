"""Catalog-addressed execution edges and an independent closure obligation.

Object addresses include their catalog: unrelated OIDs are never conflated.
The five relation-bearing expression classes are closed. Local column/storage
ownership links are checked structurally, not treated as executable SQL edges.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from django.db import connection

Address = tuple[str, int]
EXPRESSION_CLASSES = frozenset(
    {"pg_rewrite", "pg_policy", "pg_proc", "pg_trigger", "pg_constraint"}
)
# These objects carry identity/configuration, not relation execution. Types are
# separately expanded through the live type graph, including domain expressions.
TERMINAL_CLASSES = frozenset(
    {"pg_namespace", "pg_language", "pg_extension", "pg_collation"}
)


@dataclass(frozen=True)
class References:
    functions: frozenset[int] = frozenset()
    relations: frozenset[int] = frozenset()
    types: frozenset[int] = frozenset()


class RelationEdges:
    def __init__(self) -> None:
        self.dependencies: dict[Address, set[Address]] = defaultdict(set)
        self.resolved: dict[Address, References] = {}
        self.incoming: dict[int, set[Address]] = defaultdict(set)
        self.structural: dict[Address, set[int]] = defaultdict(set)
        self.relations: set[int] = set()
        self.objects: dict[str, Address] = {}
        self.owned: dict[int, set[Address]] = defaultdict(set)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT 'policy:'||polrelid::regclass::text||'.'||polname,"
                "'pg_policy',oid,polrelid FROM pg_policy UNION ALL "
                "SELECT 'rule:'||ev_class::regclass::text||'.'||rulename,"
                "'pg_rewrite',oid,ev_class FROM pg_rewrite UNION ALL "
                "SELECT 'trigger:'||tgrelid::regclass::text||'.'||tgname,"
                "'pg_trigger',oid,tgrelid FROM pg_trigger "
                "WHERE NOT tgisinternal UNION ALL "
                "SELECT 'constraint:'||conrelid::regclass::text||'.'||conname,"
                "'pg_constraint',oid,conrelid FROM pg_constraint WHERE contype='f'"
            )
            for key, catalog, oid, owner in cursor.fetchall():
                address = (str(catalog), int(oid))
                self.objects[str(key)] = address
                self.owned[int(owner)].add(address)
            cursor.execute(
                "SELECT classid::regclass::text,objid,"
                "refclassid::regclass::text,refobjid FROM pg_depend"
            )
            for catalog, oid, target_catalog, target in cursor.fetchall():
                address = (str(catalog), int(oid))
                self.dependencies[address].add((str(target_catalog), int(target)))
                if target_catalog == "pg_class":
                    self.incoming[int(target)].add(address)
            cursor.execute("SELECT oid,oprcode::oid FROM pg_operator")
            self.operators = {
                int(oid): int(function) for oid, function in cursor.fetchall()
            }
            cursor.execute(
                "SELECT oid FROM pg_class WHERE relkind IN ('r','p','v','m','f')"
            )
            self.relations = {int(row[0]) for row in cursor.fetchall()}
            cursor.execute(
                "SELECT 'pg_attrdef',oid,adrelid FROM pg_attrdef UNION ALL "
                "SELECT 'pg_type',oid,typrelid FROM pg_type "
                "WHERE typrelid<>0 UNION ALL "
                "SELECT 'pg_class',indexrelid,indrelid FROM pg_index UNION ALL "
                "SELECT 'pg_class',reltoastrelid,oid FROM pg_class "
                "WHERE reltoastrelid<>0 UNION ALL "
                "SELECT 'pg_class',inhrelid,inhparent FROM pg_inherits UNION ALL "
                "SELECT 'pg_class',partrelid,partrelid FROM pg_partitioned_table "
                "WHERE partexprs IS NULL UNION ALL "
                "SELECT 'pg_class',d.objid,d.refobjid FROM pg_depend d "
                "JOIN pg_class c ON c.oid=d.objid AND c.relkind='S' "
                "WHERE d.classid='pg_class'::regclass "
                "AND d.refclassid='pg_class'::regclass AND d.refobjsubid>0 "
                "AND d.deptype IN ('a','i')"
            )
            for catalog, oid, owner in cursor.fetchall():
                self.structural[(str(catalog), int(oid))].add(int(owner))

    def references(self, address: Address) -> References:
        # SQL-clone edges are registered before its surface is inspected. Reuse
        # immutable references only within this fully captured catalog instance.
        if address in self.resolved:
            return self.resolved[address]
        assert address[0] in EXPRESSION_CLASSES, (
            f"unanalysed dependency class: {address}"
        )
        found: dict[str, set[int]] = defaultdict(set)
        for catalog, oid in self.dependencies[address]:
            if catalog == "pg_operator":
                found["pg_proc"].add(self.operators[oid])
                continue
            assert catalog in TERMINAL_CLASSES | {"pg_class", "pg_proc", "pg_type"}, (
                f"unanalysed dependency class: {address} -> {(catalog, oid)}"
            )
            found[catalog].add(oid)
        result = References(
            frozenset(found["pg_proc"]),
            frozenset(found["pg_class"]),
            frozenset(found["pg_type"]),
        )
        self.resolved[address] = result
        return result

    def assert_closed(
        self,
        objects: set[Address],
        relations: set[int],
        functions: set[int],
        types: set[int],
    ) -> None:
        # Read the captured pg_depend rows independently of References: removing
        # a discovery edge must not remove its verification obligation too.
        followed = {"pg_class": relations, "pg_proc": functions, "pg_type": types}
        for address in objects:
            assert address[0] in EXPRESSION_CLASSES, (
                f"unanalysed dependency class: {address}"
            )
            for catalog, oid in self.dependencies[address]:
                if catalog in TERMINAL_CLASSES:
                    continue
                if catalog == "pg_operator":
                    assert self.operators[oid] in functions, (
                        f"unfollowed operator: {address} -> {oid}"
                    )
                    continue
                assert catalog in followed, (
                    f"unanalysed dependency class: {address} -> {(catalog, oid)}"
                )
                assert oid in followed[catalog], (
                    f"unfollowed dependency: {address} -> {(catalog, oid)}"
                )
        for relation in relations & self.relations:
            assert self.owned[relation] <= objects, (
                f"unvisited expression objects: {self.owned[relation] - objects}"
            )
            for address in self.incoming[relation]:
                assert (
                    address[0] in EXPRESSION_CLASSES
                    or relation in self.structural[address]
                ), (
                    "unanalysed dependency class on reached relation: "
                    f"{address} -> {relation}"
                )
