"""Independent static inventory for pending model reads (no endpoint allowlist)."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING

from django.apps import apps

from ai.pending_declarations import declarative_bindings

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from django.db.models import Model

BOUNDARY_MODULE = "apps.ai.pending_outputs"
RAW = "raw pending model at presentation boundary"
ISSUED = "pending projection permit issued outside boundary"
INTERNAL = "pending projection boundary internals used outside boundary"
# Where a value leaves a function for a person or a byte stream.
_RENDERERS = frozenset(
    {
        "render",
        "render_to_string",
        "JsonResponse",
        "HttpResponse",
        "StreamingHttpResponse",
        "FileResponse",
        "serialize",
        "asdict",
        "model_to_dict",
        "project",
    }
)
# A digest, a size or a truth value of pending fields discloses no field.
_OPAQUE_CALLS = frozenset(
    {"sha256", "sha512", "blake2b", "md5", "hexdigest", "any", "all", "len", "bool"}
)
_SCALAR_TYPES = frozenset({"UUID", "Decimal", "Path"})
# Wrappers that only collect values; a DTO inside one is still stored.
_WRAPPERS = frozenset({"tuple", "list", "set", "frozenset", "sorted", "dict"})
_COLLECTIONS = (
    ast.Dict,
    ast.List,
    ast.Set,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
)
_MUTATORS = frozenset(
    {"append", "appendleft", "extend", "add", "update", "insert", "setdefault"}
)
_INTERNALS = frozenset({"_ISSUER", "_CURRENT", "_VisibleOutput"})
_FRAME_STATE = frozenset(
    {
        "_records",
        "_handles",
        "_export_data",
        "_export_rows",
        "_issuer",
        "_thread",
        "_ended",
        "_key",
        "_clinic",
        "_index",
    }
)


@dataclass(frozen=True)
class Inventory:
    fields: dict[str, frozenset[str]]
    tables: frozenset[str]
    archives: frozenset[str]
    permits: frozenset[str]
    # A primary or foreign key selects a row; every other concrete column is
    # pending output once it is rendered.
    content: frozenset[str]
    keys: frozenset[str]
    pending_relations: frozenset[str]


def inventory() -> Inventory:
    from apps.ai import pending_outputs  # noqa: PLC0415
    from apps.ai.pending_outputs import PendingOutput, SealedOutput  # noqa: PLC0415

    models = [
        m for m in apps.get_models() if issubclass(m, (PendingOutput, SealedOutput))
    ]
    columns = [f for m in models for f in m._meta.concrete_fields]
    content = frozenset(
        s
        for f in columns
        if not (f.is_relation or f.primary_key)
        for s in (f.name, f.attname)
    )
    # An earlier revision with no permit type must still be scannable:
    # a real raw presenter is a finding, not an import/collection error.
    marker = vars(pending_outputs).get("_VisibleOutput")
    permits = marker.__subclasses__() if isinstance(marker, type) else ()
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
        frozenset(t.__name__ for t in permits),
        content,
        frozenset(
            {"pk"}
            | {
                s
                for f in columns
                if f.is_relation or f.primary_key
                for s in (f.name, f.attname)
            }
        )
        - content,
        frozenset(
            field.name
            for model in apps.get_models()
            for field in model._meta.get_fields()
            if field.is_relation and field.related_model in models
        ),
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
        if isinstance(node, ast.ImportFrom) and node.module == BOUNDARY_MODULE
        for imp in node.names
        if imp.name == symbol
    }


def _functions(
    tree: ast.AST,
) -> Iterator[ast.FunctionDef | ast.AsyncFunctionDef]:
    return (
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    )


def _arguments(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[ast.arg, ...]:
    args = fn.args
    return (
        *args.posonlyargs,
        *args.args,
        *args.kwonlyargs,
        *(arg for arg in (args.vararg, args.kwarg) if arg is not None),
    )


def _raw_types(
    annotation: ast.AST,
    names: dict[str, str],
    aliases: dict[str, ast.AST],
    opaque: set[str],
    seen: frozenset[str] = frozenset(),
) -> set[str]:
    """Unwrap union, collection and queryset annotations, but never a checked key."""
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        try:
            return _raw_types(
                ast.parse(annotation.value, mode="eval").body,
                names,
                aliases,
                opaque,
                seen,
            )
        except SyntaxError:
            return set()
    leaf = _leaf(annotation)
    if leaf in opaque or (
        isinstance(annotation, ast.Subscript) and _leaf(annotation.value) in opaque
    ):
        return set()
    if leaf in names:
        return {names[leaf]}
    if leaf in aliases and leaf not in seen:
        return _raw_types(aliases[leaf], names, aliases, opaque, seen | {leaf})
    return {
        found
        for child in ast.iter_child_nodes(annotation)
        for found in _raw_types(child, names, aliases, opaque, seen)
    }


def _visible(node: ast.AST) -> Iterator[ast.AST]:
    """Walk a subtree without entering a digest or a size/truth reduction."""
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, ast.Call) and _leaf(current.func) in _OPAQUE_CALLS:
            continue
        yield current
        stack.extend(ast.iter_child_nodes(current))


def _root(node: ast.AST) -> ast.Name | None:
    while isinstance(node, (ast.Attribute, ast.Subscript, ast.Call)):
        node = node.func if isinstance(node, ast.Call) else node.value
    return node if isinstance(node, ast.Name) else None


def _flows(fn: ast.AST) -> list[tuple[list[ast.expr], ast.AST]]:
    """Every binding of a value to a name, including in-place collection growth."""
    flows: list[tuple[list[ast.expr], ast.AST]] = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            flows.append((node.targets, node.value))
        elif (
            isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr))
            and node.value is not None
        ):
            flows.append(([node.target], node.value))
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
            flows.append(([node.target], node.iter))
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            flows.extend(
                ([item.optional_vars], item.context_expr)
                for item in node.items
                if item.optional_vars is not None
            )
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _MUTATORS
            and (receiver := _root(node.func.value)) is not None
        ):
            values = [*node.args, *(keyword.value for keyword in node.keywords)]
            flows.append(([receiver], ast.Tuple(elts=values, ctx=ast.Load())))
    return flows


def _propagate(
    seed: set[str],
    flows: list[tuple[list[ast.expr], ast.AST]],
    carries: Callable[[ast.AST, set[str]], bool],
) -> set[str]:
    names = set(seed)
    while True:
        before = len(names)
        for targets, source in flows:
            if carries(source, names):
                names |= {
                    n.id
                    for t in targets
                    for n in ast.walk(t)
                    if isinstance(n, ast.Name)
                }
        if len(names) == before:
            return names


def _mentions(source: ast.AST, held: set[str]) -> bool:
    return any(isinstance(n, ast.Name) and n.id in held for n in ast.walk(source))


def _reads_column(
    source: ast.AST, held: set[str], *, derived: set[str], content: frozenset[str]
) -> bool:
    """The value holds a stored column of a derived instance, or such a value."""
    return any(
        (isinstance(n, ast.Name) and n.id in held)
        or (
            isinstance(n, ast.Attribute)
            and n.attr in content
            and (root := _root(n)) is not None
            and root.id in derived
        )
        for n in ast.walk(source)
    )


def _is_dto(leaf: str) -> bool:
    return (
        leaf[:1].isupper()
        and leaf not in _SCALAR_TYPES
        and not leaf.endswith(("Error", "Exception", "Key"))
    )


def _direct_dtos(source: ast.AST) -> Iterator[ast.Call]:
    """DTOs stored as the value itself, not passed into another call."""
    stack = [source]
    while stack:
        node = stack.pop()
        if isinstance(node, ast.Call):
            leaf = _leaf(node.func)
            if _is_dto(leaf):
                yield node
                stack.extend(ast.iter_child_nodes(node))
            elif leaf in _WRAPPERS:
                stack.extend(node.args)
                stack.extend(keyword.value for keyword in node.keywords)
            continue
        stack.extend(ast.iter_child_nodes(node))


def _uses(expr: ast.AST, derived: set[str], keys: frozenset[str]) -> bool:
    """A derived value used for more than selecting its row by key."""
    parents = {
        id(child): parent
        for parent in _visible(expr)
        for child in ast.iter_child_nodes(parent)
    }
    for node in _visible(expr):
        if not (isinstance(node, ast.Name) and node.id in derived):
            continue
        parent = parents.get(id(node))
        if isinstance(parent, ast.Attribute) and parent.attr in keys:
            outer = parents.get(id(parent))
            if not (isinstance(outer, ast.Attribute) and outer.value is parent):
                continue
        return True
    return False


def _sinks(
    fn: ast.AST, flows: list[tuple[list[ast.expr], ast.AST]]
) -> Iterator[tuple[ast.AST, bool]]:
    """Yield each expression that leaves the function, flagging bare returns.

    A value leaves through a renderer, a returned DTO or collection, a bare
    returned value, or a DTO stored into a collection that is itself returned.
    A command's arguments (for example an action specification) do not.
    """
    escaping: set[str] = set()
    yield from _exits(fn, escaping)
    yield from ((call, False) for call in _stored_dtos(flows, escaping))


def _exits(fn: ast.AST, escaping: set[str]) -> Iterator[tuple[ast.AST, bool]]:
    """Returned values and renderer calls; record every name they carry."""
    for node in _visible(fn):
        if isinstance(node, (ast.Return, ast.Yield)) and node.value is not None:
            escaping |= {n.id for n in _visible(node.value) if isinstance(n, ast.Name)}
            if isinstance(node.value, (ast.Name, ast.Attribute)):
                yield node.value, True
            yield from (
                (inner, False)
                for inner in _visible(node.value)
                if isinstance(inner, _COLLECTIONS)
                or (isinstance(inner, ast.Call) and _is_dto(_leaf(inner.func)))
            )
        elif isinstance(node, ast.Call) and _leaf(node.func) in _RENDERERS:
            escaping |= {n.id for n in _visible(node) if isinstance(n, ast.Name)}
            yield node, False


def _stored_dtos(
    flows: list[tuple[list[ast.expr], ast.AST]], escaping: set[str]
) -> Iterator[ast.Call]:
    """DTOs placed directly into a value that is returned or rendered."""
    while True:
        before = len(escaping)
        for targets, source in flows:
            if any(_mentions(target, escaping) for target in targets):
                escaping |= {n.id for n in _visible(source) if isinstance(n, ast.Name)}
        if len(escaping) == before:
            break
    for targets, source in flows:
        if any(_mentions(target, escaping) for target in targets):
            yield from _direct_dtos(source)


def _stored_column(
    expr: ast.AST, derived: set[str], stored: set[str], content: frozenset[str]
) -> bool:
    if isinstance(expr, ast.Name):
        return expr.id in stored
    root = _root(expr)
    return (
        isinstance(expr, ast.Attribute)
        and expr.attr in content
        and root is not None
        and root.id in derived
    )


def _annotation_aliases(tree: ast.Module) -> dict[str, ast.AST]:
    aliases: dict[str, ast.AST] = {
        node.name.id: node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.TypeAlias) and isinstance(node.name, ast.Name)
    }
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and isinstance(
            stmt.value, (ast.Subscript, ast.BinOp)
        ):
            aliases.update(
                (target.id, stmt.value)
                for target in stmt.targets
                if isinstance(target, ast.Name)
            )
    return aliases


def _raw_projection_signatures(
    tree: ast.Module, spec: Inventory, names: dict[str, str]
) -> set[tuple[int, str]]:
    """A function given a raw model or queryset must not render or serialize it.

    Raw parameters are derived from the models and the annotations (aliases,
    unions, collections, querysets). Their values are followed through
    assignment, iteration and collection growth into any DTO, response,
    returned collection or returned stored column.
    """
    opaque = set(spec.permits | {"PendingKey"})
    for permit in tuple(opaque):
        opaque.update(_boundary_aliases(tree, permit))
    aliases = _annotation_aliases(tree)
    bad = set()
    for fn in _functions(tree):
        raw = {
            arg.arg
            for arg in _arguments(fn)
            if arg.annotation is not None
            and _raw_types(arg.annotation, names, aliases, opaque)
        }
        if not raw:
            continue
        flows = _flows(fn)
        derived = _propagate(raw, flows, _mentions)
        stored = _propagate(
            set(),
            flows,
            partial(_reads_column, derived=derived, content=spec.content),
        )
        if any(
            _stored_column(expr, derived, stored, spec.content)
            if bare
            else _uses(expr, derived, spec.keys)
            for expr, bare in _sinks(fn, flows)
        ):
            bad.add((fn.lineno, RAW))
    return bad


def _boundary_internals(
    tree: ast.Module, permits: dict[str, str]
) -> set[tuple[int, str]]:
    """The issuer token and frame state are not an API, even for a frame holder."""
    bad = {
        (node.lineno, INTERNAL)
        for node in ast.walk(tree)
        if (
            isinstance(node, ast.ImportFrom)
            and node.module == BOUNDARY_MODULE
            and any(imp.name in _INTERNALS for imp in node.names)
        )
        or (isinstance(node, ast.Attribute) and node.attr in _INTERNALS)
        or (isinstance(node, ast.Name) and node.id in _INTERNALS)
    }
    for fn in _functions(tree):
        held = {
            arg.arg
            for arg in _arguments(fn)
            if arg.annotation is not None
            and any(_leaf(n) in permits for n in ast.walk(arg.annotation))
        }
        bad |= {
            (node.lineno, INTERNAL)
            for node in ast.walk(fn)
            if isinstance(node, ast.Attribute)
            and node.attr in _FRAME_STATE
            and isinstance(node.value, ast.Name)
            and node.value.id in held
        }
    return bad


def permit_issuance(
    source: str, spec: Inventory, *, boundary: bool = False
) -> set[tuple[int, str]]:
    """Only the issuing function may construct a checked projection permit."""
    tree = ast.parse(source)
    names = _model_aliases(tree, set(spec.permits))
    issuer = (
        {
            call
            for fn in tree.body
            if isinstance(fn, ast.FunctionDef) and fn.name == "ai_pending_visible"
            for call in ast.walk(fn)
        }
        if boundary
        else set()
    )
    bad = set()
    for node in ast.walk(tree):
        if node in issuer:
            continue
        if (isinstance(node, ast.Call) and _leaf(node.func) in names) or (
            not boundary
            and isinstance(node, ast.ClassDef)
            and any(
                _leaf(base) in names or _leaf(base) == "_VisibleOutput"
                for base in node.bases
            )
        ):
            bad.add((node.lineno, ISSUED))
    if not boundary:
        bad |= _boundary_internals(tree, names)
    return bad


def checked_presenter_mutations(root: Path) -> Iterator[tuple[str, int, str]]:
    """Revert each checked presenter, one at a time, to accept its raw model.

    The set comes from the annotations: every function outside the boundary
    with a ``VisiblePending[...]`` parameter. Only that one annotation changes.
    """
    boundary = root / "apps/ai/pending_outputs.py"
    for path in sorted((root / "apps").rglob("*.py")):
        if "migrations" in path.parts or path == boundary:
            continue
        source = path.read_bytes()
        starts = [0]
        for line in source.splitlines(keepends=True):
            starts.append(starts[-1] + len(line))
        for fn in _functions(ast.parse(source)):
            permit = next(
                (
                    node
                    for arg in _arguments(fn)
                    if arg.annotation is not None
                    for node in ast.walk(arg.annotation)
                    if isinstance(node, ast.Subscript)
                    and _leaf(node.value) == "VisiblePending"
                ),
                None,
            )
            if permit is None:
                continue
            inner = permit.slice
            mutated = (
                source[: _offset(starts, permit.lineno, permit.col_offset)]
                + source[
                    _offset(starts, inner.lineno, inner.col_offset) : _offset(
                        starts, inner.end_lineno, inner.end_col_offset
                    )
                ]
                + source[_offset(starts, permit.end_lineno, permit.end_col_offset) :]
            )
            yield str(path.relative_to(root)), fn.lineno, mutated.decode()


def _offset(starts: list[int], line: int | None, column: int | None) -> int:
    """Translate an AST (line, UTF-8 byte column) pair into a byte offset."""
    assert line is not None
    assert column is not None
    return starts[line - 1] + column


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
    for fn in _functions(tree):
        typed = {
            a.arg: fields[names[_leaf(a.annotation)]]
            for a in _arguments(fn)
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


def violations(
    source: str,
    spec: Inventory,
    *,
    processors: frozenset[str] = frozenset(),
    project_families: frozenset[str] = frozenset(),
) -> set[tuple[int, str]]:
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
    bad |= _raw_projection_signatures(tree, spec, names)
    bad |= permit_issuance(source, spec)
    bad |= declarative_bindings(
        tree,
        models=set(names),
        permits=spec.permits,
        fields=(spec.pending_relations, spec.content),
        registered=(processors, project_families),
    )
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
