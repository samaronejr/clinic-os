"""Independent static inventory for pending model reads (no endpoint allowlist)."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.apps import apps

if TYPE_CHECKING:
    from django.db.models import Model


@dataclass(frozen=True)
class Inventory:
    fields: dict[str, frozenset[str]]
    tables: frozenset[str]
    archives: frozenset[str]


def inventory() -> Inventory:
    from apps.ai.pending_outputs import PendingOutput, SealedOutput  # noqa: PLC0415

    models = [
        m for m in apps.get_models() if issubclass(m, (PendingOutput, SealedOutput))
    ]
    return Inventory(
        {
            m.__name__: frozenset(
                {"pk"}
                | {s for f in m._meta.concrete_fields for s in (f.name, f.attname)}
            )
            for m in models
        },
        frozenset(m._meta.db_table for m in models if issubclass(m, PendingOutput)),
        frozenset(m._meta.db_table for m in models if issubclass(m, SealedOutput)),
    )


def unclassified() -> set[type[Model]]:
    from apps.ai.pending_outputs import OutputProvenance, PendingOutput  # noqa: PLC0415

    # Start with the gateway, not a list of its readers. Trace forward through
    # FK/O2O and require explicit classification at every new lineage edge.
    # Legacy independent generators also declare their non-FK invocation ref.
    models = set(apps.get_models())
    origins = {apps.get_model("ai", "AIInvocation"), apps.get_model("ai", "AIArtifact")}
    origins.update(
        m
        for m in models
        if any(f.name == "invocation_ref" for f in m._meta.concrete_fields)
    )
    while True:
        nxt = origins | {
            m
            for m in models
            if any(f.related_model in origins for f in m._meta.concrete_fields)
        }
        if nxt == origins:
            break
        origins = nxt
    return {m for m in origins if not issubclass(m, (PendingOutput, OutputProvenance))}


def _leaf(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.rsplit(".", 1)[-1]
    return ""


def _model_aliases(tree: ast.Module, model_names: set[str]) -> dict[str, str]:
    names = {name: name for name in model_names}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for imp in node.names:
                if imp.name in model_names:
                    names[imp.asname or imp.name] = imp.name
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and _leaf(node.value) in names:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names[target.id] = names[_leaf(node.value)]
    return names


def _boundary_aliases(tree: ast.Module, symbol: str) -> set[str]:
    return {
        imp.asname or imp.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "apps.ai.pending_outputs"
        for imp in node.names
        if imp.name == symbol
    }


def _field_sinks(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.AST]:
    sinks: list[ast.AST] = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Dict):
            sinks.extend(node.values)
        elif isinstance(node, ast.Call) and _leaf(node.func) == "dict":
            sinks.extend(node.args)
            sinks.extend(kw.value for kw in node.keywords)
        elif isinstance(node, ast.Return) and isinstance(
            node.value, (ast.Attribute, ast.Tuple, ast.List)
        ):
            sinks.append(node.value)
    return sinks


def _direct_field_projections(
    tree: ast.Module, fields: dict[str, frozenset[str]], names: dict[str, str]
) -> set[tuple[int, str]]:
    selectors = _boundary_aliases(tree, "reselect")
    bad = set()
    for fn in (
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ):
        arguments = (*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs)
        typed = {
            a.arg: fields[names[_leaf(a.annotation)]]
            for a in arguments
            if a.annotation and _leaf(a.annotation) in names
        }
        # Only an unconditional re-read of the same argument in this function
        # can authorize a retained model, and it must precede serialization.
        selected = {
            t.id: stmt.lineno
            for stmt in fn.body
            if isinstance(stmt, ast.Assign)
            and isinstance(stmt.value, ast.Call)
            and _leaf(stmt.value.func) in selectors
            for t in stmt.targets
            if isinstance(t, ast.Name)
            and stmt.value.args
            and _leaf(stmt.value.args[0]) == t.id
        }
        for sink in _field_sinks(fn):
            for attr in (a for a in ast.walk(sink) if isinstance(a, ast.Attribute)):
                obj = _leaf(attr.value)
                if (
                    attr.attr in typed.get(obj, ())
                    and selected.get(obj, attr.lineno) >= attr.lineno
                ):
                    bad.add(
                        (attr.lineno, "pending field serialized without reselection")
                    )
    return bad


def violations(source: str, spec: Inventory) -> set[tuple[int, str]]:
    """Flag independent ORM/SQL reads and generic model serialization.

    Formatters may consume rows obtained through the data boundary. Live
    restrictive policies also cover joins and reverse managers where syntax
    cannot prove the run-time receiver type.
    """
    tree = ast.parse(source)
    names = _model_aliases(tree, set(spec.fields))
    serializers = _model_aliases(tree, {"model_to_dict", "serialize", "vars", "asdict"})
    tables = "|".join(re.escape(t) for t in spec.tables | spec.archives)
    sql = re.compile(
        rf"\b(?:from|join)\s+(?:clinic_app\.)?(?:{tables})\b", re.IGNORECASE
    )
    instances = {
        node.arg: names[_leaf(node.annotation)]
        for node in ast.walk(tree)
        if isinstance(node, ast.arg)
        and node.annotation
        and _leaf(node.annotation) in names
    }
    bad = _direct_field_projections(tree, spec.fields, names)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr in {"objects", "_base_manager", "_default_manager", "raw"}
            and (
                _leaf(node.value) in names
                or (
                    isinstance(node.value, ast.Call)
                    and _leaf(node.value.func) == "get_model"
                )
            )
        ):
            bad.add((node.lineno, "direct pending model query"))
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and (
                sql.search(node.value.replace('"', ""))
                or "interop_export_rows(" in node.value
            )
        ):
            bad.add((node.lineno, "raw pending projection"))
        if (
            isinstance(node, ast.Call)
            and _leaf(node.func) in serializers
            and any(
                isinstance(v, ast.Name) and (v.id in instances or v.id in names)
                for expr in (*node.args, *(kw.value for kw in node.keywords))
                for v in ast.walk(expr)
            )
        ):
            bad.add((node.lineno, "direct pending serialization"))
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "__dict__"
            and (_leaf(node.value) in instances)
        ):
            bad.add((node.lineno, "direct pending serialization"))
    return bad
