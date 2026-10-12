"""The catalog itself supplies the closure obligations and unknown edge kinds."""

from __future__ import annotations

from dataclasses import replace

import pytest

from .clock_catalog import _build_inventory, live_clock_inventory
from .clock_expression_probes import POLICY, RULE
from .clock_relation_edges import EXPRESSION_CLASSES, Address, References, RelationEdges
from .clock_residual_probes import Probe
from .test_residual_clock_probes import RESTORATION, installed_probe

pytestmark = [
    pytest.mark.django_db(transaction=True, available_apps=[]),
    pytest.mark.usefixtures("clock_catalog_session"),
]


def test_catalog_obligation_detects_a_missing_edge(
    superuser_database_url: str,
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipts: list[
        tuple[RelationEdges, set[Address], set[int], set[int], set[int]]
    ] = []
    original = RelationEdges.assert_closed

    def capture(
        self: RelationEdges,
        objects: set[Address],
        relations: set[int],
        functions: set[int],
        types: set[int],
    ) -> None:
        original(self, objects, relations, functions, types)
        receipts.append((self, objects, relations, functions, types))

    with installed_probe(superuser_database_url, RULE, request):
        with monkeypatch.context() as patch:
            patch.setattr(RelationEdges, "assert_closed", capture)
            _build_inventory()
        assert len(receipts) == 1
        graph, objects, relations, functions, types = receipts[0]
        refused = []
        for catalog in sorted(EXPRESSION_CLASSES):
            address = next(
                address
                for address in sorted(objects)
                if address[0] == catalog
                and any(
                    kind in {"pg_class", "pg_proc"}
                    for kind, _ in graph.dependencies[address]
                )
            )
            kind, oid = next(
                (kind, oid)
                for kind, oid in sorted(graph.dependencies[address])
                if kind in {"pg_class", "pg_proc"}
            )
            followed = {"pg_class": set(relations), "pg_proc": set(functions)}
            followed[kind].remove(oid)
            with pytest.raises(AssertionError, match="unfollowed dependency"):
                graph.assert_closed(
                    objects, followed["pg_class"], followed["pg_proc"], types
                )
            if catalog != "pg_proc":
                with pytest.raises(
                    AssertionError, match="unvisited expression objects"
                ):
                    graph.assert_closed(
                        objects - {address}, relations, functions, types
                    )
            refused.append(catalog)
        assert set(refused) == EXPRESSION_CLASSES
        request.node.stash[RESTORATION]["closed_classes"] = ",".join(refused)


@pytest.mark.parametrize(
    ("probe", "catalog"),
    [(RULE, "pg_rewrite"), (POLICY, "pg_policy")],
    ids=["rule", "policy"],
)
def test_missing_expression_relation_fails_closure(
    probe: Probe,
    catalog: str,
    superuser_database_url: str,
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with installed_probe(superuser_database_url, probe, request):
        original = RelationEdges.references

        def omitted(self: RelationEdges, address: Address) -> References:
            result = original(self, address)
            return (
                replace(result, relations=frozenset())
                if address[0] == catalog
                else result
            )

        with monkeypatch.context() as patch:
            patch.setattr(RelationEdges, "references", omitted)
            with pytest.raises(AssertionError, match="unfollowed dependency"):
                _build_inventory()
        assert live_clock_inventory() == _build_inventory()
        request.node.stash[RESTORATION]["closure_mutant_refused"] = "true"


def test_unrecognised_reached_dependency_class_is_refused(
    superuser_database_url: str,
    request: pytest.FixtureRequest,
) -> None:
    probe = Probe(
        "B3-DEPENDENCY-CLASS",
        "CREATE SCHEMA r9; CREATE TABLE r9.zq_unknown(v integer); "
        "CREATE FUNCTION clinic_app.scheduling_r9_root() RETURNS integer "
        "LANGUAGE sql AS $$ SELECT v FROM r9.zq_unknown LIMIT 1 $$; "
        "GRANT USAGE ON SCHEMA r9 TO clinic_owner; "
        "GRANT ALL ON ALL TABLES IN SCHEMA r9 TO clinic_owner; "
        "CREATE PUBLICATION r9_unknown_publication FOR TABLE r9.zq_unknown;",
        "DROP PUBLICATION r9_unknown_publication; "
        "DROP FUNCTION clinic_app.scheduling_r9_root(); DROP SCHEMA r9 CASCADE;",
        "SELECT clinic_app.scheduling_r9_root() IS NULL",
    )
    with installed_probe(superuser_database_url, probe, request):
        with pytest.raises(
            AssertionError, match=r"unanalysed dependency class.*pg_publication_rel"
        ):
            live_clock_inventory()
        request.node.stash[RESTORATION]["unknown_class_refused"] = "true"
