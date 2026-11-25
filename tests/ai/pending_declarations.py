"""Detect framework bindings that materialize rows without a presenter call."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from rest_framework import generics, views, viewsets

if TYPE_CHECKING:
    from collections.abc import Iterable

SERIALIZERS = {
    "Serializer",
    "ModelSerializer",
    "HyperlinkedModelSerializer",
    "ListSerializer",
}
ADMINS = {"ModelAdmin", "InlineModelAdmin", "TabularInline", "StackedInline"}
FORMS = {"Form", "ModelForm", "BaseModelForm", "BaseModelFormSet", "BaseInlineFormSet"}
# Also cover a source-only (not yet importable or routed) generic view that
# already commits to raw pending input, including aliases of framework bases.
DRF_VIEWS = {
    value.__name__
    for module in (generics, views, viewsets)
    for value in vars(module).values()
    if isinstance(value, type) and issubclass(value, views.APIView)
}
FAMILIES = SERIALIZERS | ADMINS | FORMS | DRF_VIEWS
FACTORIES = {"modelform_factory", "modelformset_factory", "inlineformset_factory"}
DECLARATIVE = "raw pending model at declarative binding"


def leaf(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript):
        return leaf(node.value)
    return ""


@dataclass
class Bindings:
    models: set[str]
    opaque: set[str]
    families: set[str]
    factories: set[str]
    raw_fields: set[str]
    content: frozenset[str]
    serializers: set[str]
    processors: frozenset[str]

    def model(self, expr: ast.AST) -> bool:
        if leaf(expr) in self.opaque or (
            isinstance(expr, ast.Call) and leaf(expr.func) in self.opaque
        ):
            return False
        if leaf(expr) in self.models:
            return True
        if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
            # Quoted type annotations/forward references; never interpret code.
            if any(x in self.opaque for x in re.findall(r"\w+", expr.value)):
                try:
                    parsed = ast.parse(expr.value, mode="eval").body
                except SyntaxError:
                    return True
                if not isinstance(parsed, ast.Constant):
                    return self.model(parsed)
            return bool(self.models.intersection(re.findall(r"\w+", expr.value)))
        return any(self.model(n) for n in ast.iter_child_nodes(expr))


def _names(tree: ast.Module, originals: set[str]) -> set[str]:
    names = set(originals)
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            names.update(a.asname or a.name for a in n.names if a.name in originals)
    prior = -1
    while prior != len(names):
        prior = len(names)
        for n in ast.walk(tree):
            if isinstance(n, ast.Assign) and leaf(n.value) in names:
                names.update(t.id for t in n.targets if isinstance(t, ast.Name))
            if isinstance(n, ast.ClassDef) and any(leaf(b) in names for b in n.bases):
                names.add(n.name)
    return names


def framework_classes(sources: Iterable[str]) -> frozenset[str]:
    """Include imported application subclasses, even if declared in another file."""
    pending: set[tuple[str, str]] = set()
    for source in sources:
        tree = ast.parse(source)
        aliases = {
            a.asname or a.name: a.name
            for n in ast.walk(tree)
            if isinstance(n, ast.ImportFrom)
            for a in n.names
        }
        for n in ast.walk(tree):
            if isinstance(n, ast.ClassDef):
                pending.update((n.name, aliases.get(leaf(b), leaf(b))) for b in n.bases)
    discovered = set(FAMILIES)
    while extra := {name for name, base in pending if base in discovered} - discovered:
        discovered.update(extra)
    return frozenset(discovered)


def _class_binding(cls: ast.ClassDef, b: Bindings) -> bool:
    if cls.name not in b.families:
        return False
    # Base type arguments, register decorators, Model[Form|Serializer].Meta,
    # inline.model, sourced fields, and form/queryset field declarations.
    values: list[ast.AST] = [*cls.bases, *cls.decorator_list]
    for stmt in cls.body:
        if isinstance(stmt, ast.ClassDef) and stmt.name == "Meta":
            values.extend(stmt.body)
        elif isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            values.append(stmt)
        elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if stmt.name == "get_queryset":
                values.extend(stmt.body)
                if stmt.returns:
                    values.append(stmt.returns)
            values.extend(
                arg.annotation
                for arg in (
                    *stmt.args.posonlyargs,
                    *stmt.args.args,
                    *stmt.args.kwonlyargs,
                )
                if arg.annotation
            )
    if any(b.model(expr) for expr in values):
        return True
    # A plain Serializer may use a sourced relation, without either a model
    # annotation or Meta.model. Recognize pending Django relation accessors.
    return any(_related_binding(expr, b.raw_fields) for expr in values) or (
        cls.name in b.serializers
        and not any(
            isinstance(base, ast.Subscript)
            and leaf(base.slice) not in {"Any", "object"}
            for base in cls.bases
        )
        and any(_untyped_source(expr, b.content) for expr in values)
    )


def _untyped_source(expr: ast.AST, content: frozenset[str]) -> bool:
    # Without an explicit DTO type, a wildcard or pending-field source cannot
    # prove that an unmounted serializer is safe to bind to a model instance.
    return any(
        kw.arg == "source"
        and (
            not isinstance(kw.value, ast.Constant)
            or not isinstance(kw.value.value, str)
            or kw.value.value == "*"
            or bool(content.intersection(kw.value.value.split(".")))
        )
        for kw in ast.walk(expr)
        if isinstance(kw, ast.keyword)
    )


def _related_binding(expr: ast.AST, relations: set[str]) -> bool:
    # Fields can traverse a related pending model even on a *different*
    # root model, e.g. source='draft.sections' or list_display='draft__...'.
    for node in ast.walk(expr):
        args: list[ast.AST] = []
        if isinstance(node, ast.keyword) and node.arg == "source":
            args = [node.value]
        if isinstance(node, ast.Assign) and any(
            leaf(t) in {"list_display", "readonly_fields", "fields", "fieldsets"}
            for t in node.targets
        ):
            args = [node.value]
        for arg in args:
            for entry in ast.walk(arg):
                if (
                    isinstance(entry, ast.Constant)
                    and isinstance(entry.value, str)
                    and relations.intersection(
                        re.split(r"\.|__", entry.value)[:-1]
                        if "." in entry.value or "__" in entry.value
                        else ()
                    )
                ):
                    return True
    return False


def _raw_names(fn: ast.FunctionDef | ast.AsyncFunctionDef, b: Bindings) -> set[str]:
    raw = {
        a.arg
        for a in (*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs)
        if a.annotation is not None and b.model(a.annotation)
    }
    for n in ast.walk(fn):
        if (
            isinstance(n, ast.AnnAssign)
            and isinstance(n.target, ast.Name)
            and b.model(n.annotation)
        ):
            raw.add(n.target.id)
    prior = -1
    while prior != len(raw):
        prior = len(raw)
        for n in ast.walk(fn):
            if isinstance(n, ast.Assign) and (
                _mentions(n.value, raw) or b.model(n.value)
            ):
                raw.update(t.id for t in n.targets if isinstance(t, ast.Name))
    return raw


def _mentions(node: ast.AST, names: set[str]) -> bool:
    return any(isinstance(n, ast.Name) and n.id in names for n in ast.walk(node))


def _processor(
    fn: ast.FunctionDef | ast.AsyncFunctionDef, names: frozenset[str]
) -> bool:
    # A Django processor can have any name. Also scan unmounted processor-
    # shaped functions rather than waiting until settings register them.
    return fn.name in names or any(a.arg == "request" for a in fn.args.args)


def declarative_bindings(
    tree: ast.Module,
    *,
    models: set[str],
    permits: Iterable[str],
    fields: tuple[frozenset[str], frozenset[str]],
    registered: tuple[frozenset[str], frozenset[str]],
) -> set[tuple[int, str]]:
    processors, project_families = registered
    b = Bindings(
        models,
        _names(tree, {"PendingKey", *permits}),
        _names(tree, FAMILIES | set(project_families)),
        _names(tree, FACTORIES),
        set(fields[0]),
        fields[1],
        _names(tree, SERIALIZERS),
        processors,
    )
    bad = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and _class_binding(node, b):
            bad.add((node.lineno, DECLARATIVE))
        if (
            isinstance(node, ast.Call)
            and leaf(node.func) in b.factories | {"register"}
            and b.model(node)
        ):
            bad.add((node.lineno, DECLARATIVE))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            bad.update(_bound_outputs(node, b))
    return bad


def _bound_outputs(
    node: ast.FunctionDef | ast.AsyncFunctionDef, b: Bindings
) -> set[tuple[int, str]]:
    raw = _raw_names(node, b)
    mappings = {
        t.id
        for st in ast.walk(node)
        if isinstance(st, ast.Assign)
        and (
            isinstance(st.value, ast.Dict)
            or (isinstance(st.value, ast.Call) and leaf(st.value.func) == "dict")
        )
        for t in st.targets
        if isinstance(t, ast.Name)
    }
    bad = set()
    for val in ast.walk(node):
        if (
            isinstance(val, ast.Call)
            and leaf(val.func) in b.families
            and any(
                _mentions(v, raw) or b.model(v)
                for v in [*val.args, *(kw.value for kw in val.keywords)]
            )
        ):
            bad.add((val.lineno, DECLARATIVE))
        if (
            isinstance(val, ast.Return)
            and isinstance(val.value, (ast.Dict, ast.Call, ast.Name))
            and _processor(node, b.processors)
            and (
                not isinstance(val.value, ast.Name)
                or val.value.id in mappings
                or node.name in b.processors
            )
            and (not isinstance(val.value, ast.Call) or leaf(val.value.func) == "dict")
            and (_mentions(val.value, raw) or b.model(val.value))
        ):
            bad.add((val.lineno, DECLARATIVE))
    return bad
