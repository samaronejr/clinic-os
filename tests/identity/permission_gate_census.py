"""Static cross-check of permission-gated runtime functions.

This graph is an early warning, not the authority. Static analysis cannot
prove that a function has NO permission gate (a partial, a table, an
instance ``__call__``, a trigger or an unqualified SQL call all hide one),
so an exemption is valid only when an executed differential probe shows an
identical decision across every staff role state (identity/
exemption_probes.py). The census fails when this graph and a probe
disagree: a function the graph derives as gated can never be exempt.

The derived set starts from the live catalog (``clinic_app.has_permission``,
SQL functions whose bodies reach it, tables whose RLS policies call one) and
closes transitively over references resolved through imports, aliases,
module attributes, ``self``/``cls`` methods, decorators and function values.

It fails closed on what it cannot read: an opaque non-extension SQL
function, a relative import, a name that is neither local, a parameter, a
builtin, module-level nor imported, an attribute or import a project module
does not define, and (for exempt rows) reachable dynamic dispatch without a
reviewed ``DYNAMIC_DISPATCH_REVIEWED`` entry. Module-level bindings count as
bound names but their values are not followed: that is the probe's job.
"""

from __future__ import annotations

import ast
import builtins
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from django.db.backends.utils import CursorWrapper

ROOT: Final = Path(__file__).resolve().parents[2]
# The permission-bundle decision (todo 6). Everything else is derived from it.
ROOT_DECISION: Final = "has_permission"
_SQL_CALL: Final = re.compile(r"clinic_app\.([a-z_][a-z0-9_]*)\s*\(")
# The old spelling list, kept only as a cross-check of the derived set.
_SPELLED_GATE: Final = re.compile(
    r"\b(?:require_permission|authorized_enrollment_for)\("
)
_DYNAMIC_CALLS: Final = frozenset(
    {"import_module", "__import__", "eval", "exec", "globals", "locals"}
)
_MAX_REEXPORT_DEPTH: Final = 8
# Reviewed exemptions for exempt rows whose reachable code dispatches
# dynamically: symbol -> reason. Empty entries fail closed.
DYNAMIC_DISPATCH_REVIEWED: Final[Mapping[str, str]] = {}


class CensusError(AssertionError):
    """The census met something it cannot classify."""


@dataclass(frozen=True, slots=True)
class LiveDecisions:
    """Permission decisions read from the live database catalog."""

    functions: frozenset[str]
    tables: frozenset[str]


def live_decisions(cursor: CursorWrapper) -> LiveDecisions:
    """Read the permission decisions from pg_proc and pg_policy.

    SQL functions reach the root through their bodies (transitively);
    tables reach it through RLS policy expressions. An opaque SQL function
    (neither ``sql`` nor ``plpgsql``) that is not extension-owned cannot be
    inspected and fails the census.
    """
    cursor.execute(
        "SELECT p.proname, l.lanname, p.prosrc, "
        "EXISTS (SELECT 1 FROM pg_catalog.pg_depend AS d "
        "        WHERE d.classid = 'pg_catalog.pg_proc'::pg_catalog.regclass "
        "          AND d.objid = p.oid AND d.deptype = 'e') "
        "FROM pg_catalog.pg_proc AS p "
        "JOIN pg_catalog.pg_namespace AS n ON n.oid = p.pronamespace "
        "JOIN pg_catalog.pg_language AS l ON l.oid = p.prolang "
        "WHERE n.nspname = 'clinic_app'"
    )
    bodies: dict[str, str] = {}
    for name, language, source, extension in cursor.fetchall():
        if language in ("sql", "plpgsql"):
            bodies[str(name)] = bodies.get(str(name), "") + str(source)
        elif not extension:
            message = f"opaque SQL function clinic_app.{name} ({language})"
            raise CensusError(message)
    if ROOT_DECISION not in bodies:
        message = f"permission decision clinic_app.{ROOT_DECISION} is missing"
        raise CensusError(message)
    decisions = {ROOT_DECISION}
    grown = True
    while grown:
        grown = False
        for name, source in bodies.items():
            if name not in decisions and decisions & set(_SQL_CALL.findall(source)):
                decisions.add(name)
                grown = True
    cursor.execute(
        "SELECT c.relname, "
        "COALESCE(pg_catalog.pg_get_expr(p.polqual, p.polrelid), '') || ' ' || "
        "COALESCE(pg_catalog.pg_get_expr(p.polwithcheck, p.polrelid), '') "
        "FROM pg_catalog.pg_policy AS p "
        "JOIN pg_catalog.pg_class AS c ON c.oid = p.polrelid "
        "JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'clinic_app'"
    )
    tables = {
        str(table)
        for table, expression in cursor.fetchall()
        if decisions & set(re.findall(r"([a-z_][a-z0-9_]*)\s*\(", str(expression)))
    }
    return LiveDecisions(frozenset(decisions), frozenset(tables))


_BUILTIN_NAMES: Final = frozenset(dir(builtins)) | {
    "__name__",
    "__file__",
    "__doc__",
    "__package__",
    "__spec__",
    "__class__",
}


@dataclass(slots=True)
class _Module:
    name: str
    defs: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = field(
        default_factory=dict
    )
    classes: dict[str, set[str]] = field(default_factory=dict)
    imports: dict[str, str] = field(default_factory=dict)
    bound: set[str] = field(default_factory=set)
    tables: dict[str, str] = field(default_factory=dict)


def _module_name(path: Path) -> str:
    relative = path.relative_to(ROOT).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _collect(name: str, tree: ast.Module) -> _Module:
    module = _Module(name)
    for node in tree.body:
        _collect_node(module, node)
    return module


def _collect_node(module: _Module, node: ast.stmt) -> None:
    """Record one module-level statement's definitions and bindings."""
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        module.defs[node.name] = node
        module.bound.add(node.name)
    elif isinstance(node, ast.ClassDef):
        _collect_class(module, node)
    elif isinstance(node, ast.Import | ast.ImportFrom):
        _collect_import(module, node)
    else:
        _collect_statement(module, node)


def _collect_statement(module: _Module, node: ast.stmt) -> None:
    """Assignments, loops, with/try/if blocks: bind targets and descend."""
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store):
            module.bound.add(child.id)
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.stmt):
            _collect_node(module, child)
        elif isinstance(child, ast.ExceptHandler):
            if child.name:
                module.bound.add(child.name)
            for statement in child.body:
                _collect_node(module, statement)


def _collect_class(module: _Module, node: ast.ClassDef) -> None:
    methods: set[str] = set()
    for item in node.body:
        if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef):
            module.defs[f"{node.name}.{item.name}"] = item
            methods.add(item.name)
    module.classes[node.name] = methods
    module.bound.add(node.name)
    table = _db_table(node)
    if table:
        module.tables[node.name] = table


def _collect_import(module: _Module, node: ast.Import | ast.ImportFrom) -> None:
    if isinstance(node, ast.Import):
        for alias in node.names:
            bound = alias.asname or alias.name.split(".", 1)[0]
            module.imports[bound] = alias.name if alias.asname else bound
            module.bound.add(bound)
        return
    if node.level:
        message = f"relative import in {module.name} is not resolved"
        raise CensusError(message)
    for alias in node.names:
        if alias.name == "*":
            message = f"star import in {module.name} is not resolved"
            raise CensusError(message)
        bound = alias.asname or alias.name
        module.imports[bound] = f"{node.module}.{alias.name}"
        module.bound.add(bound)


def _db_table(node: ast.ClassDef) -> str:
    """Return the Django table of a model class (explicit or default)."""
    for item in node.body:
        if isinstance(item, ast.ClassDef) and item.name == "Meta":
            for statement in item.body:
                if (
                    isinstance(statement, ast.Assign)
                    and any(
                        isinstance(target, ast.Name) and target.id == "db_table"
                        for target in statement.targets
                    )
                    and isinstance(statement.value, ast.Constant)
                ):
                    return str(statement.value.value)
    return ""


@dataclass(slots=True)
class Graph:
    """Resolved reference graph over every runtime function in ``apps``."""

    modules: dict[str, _Module]
    edges: dict[str, set[str]]
    seeds: dict[str, str]
    dynamic: dict[str, str]
    unresolved: dict[str, set[str]] = field(default_factory=dict)


def _app_label(module: str) -> str:
    parts = module.split(".")
    return parts[1] if len(parts) > 1 and parts[0] == "apps" else ""


def build_graph(
    decisions: LiveDecisions, sources: Mapping[str, str] | None = None
) -> Graph:
    """Parse ``apps`` (``sources`` overrides file text by module name)."""
    modules: dict[str, _Module] = {}
    for path in sorted((ROOT / "apps").rglob("*.py")):
        if "migrations" in path.parts:
            continue
        name = _module_name(path)
        text = (sources or {}).get(name)
        modules[name] = _collect(
            name, ast.parse(path.read_text() if text is None else text)
        )
    graph = Graph(modules, {}, {}, {})
    resolver = _Resolver(modules)
    tables = _model_tables(modules)
    for module in modules.values():
        for target in module.imports.values():
            if resolver.classify_global(target, 0)[0] == "unresolved":
                graph.unresolved.setdefault(module.name, set()).add(target)
        for qualname, node in module.defs.items():
            _link(graph, resolver, module, qualname, node, decisions, tables)
    return graph


def _model_tables(modules: Mapping[str, _Module]) -> dict[str, str]:
    """Map each model class to its table (explicit, else Django's default)."""
    tables: dict[str, str] = {}
    for module in modules.values():
        for cls in module.classes:
            explicit = module.tables.get(cls, "")
            if explicit or module.name.endswith(".models"):
                tables[f"{module.name}.{cls}"] = (
                    explicit or f"{_app_label(module.name)}_{cls.lower()}"
                )
    return tables


def _link(  # noqa: PLR0913 - one function's edges need the whole context
    graph: Graph,
    resolver: _Resolver,
    module: _Module,
    qualname: str,
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    decisions: LiveDecisions,
    tables: Mapping[str, str],
) -> None:
    symbol = f"{module.name}.{qualname}"
    owner = qualname.split(".", 1)[0] if "." in qualname else ""
    local = _local_bindings(node)
    targets: set[str] = set()
    for dotted in _references(node):
        head = dotted.split(".", 1)[0]
        if head in local and head not in ("self", "cls"):
            continue
        kind, target = resolver.classify(module, dotted, owner)
        if kind == "unresolved":
            graph.unresolved.setdefault(symbol, set()).add(dotted)
        elif kind == "edge" and target is not None:
            targets.add(target)
            if tables.get(target) in decisions.tables:
                graph.seeds[symbol] = f"touches RLS-gated {tables[target]}"
    graph.edges[symbol] = targets
    for called in _sql_calls(node):
        if called in decisions.functions:
            graph.seeds[symbol] = f"executes clinic_app.{called}"
    reason = _dynamic_dispatch(node)
    if reason:
        graph.dynamic[symbol] = reason


def _local_bindings(node: ast.AST) -> set[str]:
    """Every name bound anywhere inside a function (nested scopes included)."""
    bound: set[str] = set()
    for child in ast.walk(node):
        bound.update(_bound_by(child, node))
    return bound


def _bound_by(child: ast.AST, root: ast.AST) -> tuple[str, ...]:  # noqa: PLR0911 - one binding form per branch
    if isinstance(child, ast.arguments):
        return tuple(
            argument.arg
            for argument in (
                *child.posonlyargs,
                *child.args,
                *child.kwonlyargs,
                *((child.vararg,) if child.vararg else ()),
                *((child.kwarg,) if child.kwarg else ()),
            )
        )
    if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store | ast.Del):
        return (child.id,)
    if isinstance(child, ast.Import | ast.ImportFrom):
        return tuple(
            alias.asname or alias.name.split(".", 1)[0] for alias in child.names
        )
    if isinstance(child, ast.Global | ast.Nonlocal):
        return tuple(child.names)
    if isinstance(child, ast.TypeVar | ast.ParamSpec | ast.TypeVarTuple):
        return (child.name,)
    if isinstance(child, ast.MatchMapping):
        return (child.rest,) if child.rest else ()
    named = getattr(child, "name", None)
    if isinstance(child, ast.ExceptHandler | ast.MatchAs | ast.MatchStar) or (
        isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
        and child is not root
    ):
        return (named,) if isinstance(named, str) else ()
    return ()


def _references(node: ast.AST) -> Iterator[str]:
    """Yield every dotted name the function body or decorators load."""
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
            yield child.id
        elif isinstance(child, ast.Attribute):
            chain: list[str] = [child.attr]
            base: ast.expr = child.value
            while isinstance(base, ast.Attribute):
                chain.append(base.attr)
                base = base.value
            if isinstance(base, ast.Name):
                chain.append(base.id)
                yield ".".join(reversed(chain))


def _sql_calls(node: ast.AST) -> set[str]:
    return {
        name
        for child in ast.walk(node)
        if isinstance(child, ast.Constant) and isinstance(child.value, str)
        for name in _SQL_CALL.findall(child.value)
    }


def _dynamic_dispatch(node: ast.AST) -> str:
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        name = (
            func.id
            if isinstance(func, ast.Name)
            else func.attr
            if isinstance(func, ast.Attribute)
            else ""
        )
        if name in _DYNAMIC_CALLS:
            return f"calls {name}()"
        if name == "getattr" and not (
            len(child.args) > 1 and isinstance(child.args[1], ast.Constant)
        ):
            return "getattr() with a computed attribute name"
    return ""


type _Resolution = tuple[str, str | None]


class _Resolver:
    """Classify references: edge, bound, external or unresolved."""

    def __init__(self, modules: Mapping[str, _Module]) -> None:
        self.modules = modules

    def classify(self, module: _Module, dotted: str, owner: str) -> _Resolution:
        head, _, rest = dotted.partition(".")
        if head in ("self", "cls"):
            return self._own(module, owner, rest)
        if head in module.defs and not rest:
            return ("edge", f"{module.name}.{head}")
        if head in module.classes:
            return ("edge", self._in_class(module, head, rest))
        if head in module.imports:
            target = module.imports[head] + (f".{rest}" if rest else "")
            return self.classify_global(target, 0)
        if head in module.bound or head in _BUILTIN_NAMES:
            return ("bound", None)
        return ("unresolved", None)

    @staticmethod
    def _own(module: _Module, owner: str, rest: str) -> _Resolution:
        method = rest.split(".", 1)[0]
        if owner and method in module.classes.get(owner, set()):
            return ("edge", f"{module.name}.{owner}.{method}")
        return ("bound", None)

    def _in_class(self, module: _Module, cls: str, rest: str) -> str:
        method = rest.split(".", 1)[0] if rest else ""
        if method in module.classes[cls]:
            return f"{module.name}.{cls}.{method}"
        return f"{module.name}.{cls}"

    def classify_global(self, dotted: str, depth: int) -> _Resolution:
        """Resolve an absolute dotted name, following re-exports."""
        if depth > _MAX_REEXPORT_DEPTH:
            return ("unresolved", None)
        parts = dotted.split(".")
        if parts[0] != "apps":
            return ("external", None)
        for cut in range(len(parts), 0, -1):
            candidate = ".".join(parts[:cut])
            if candidate in self.modules:
                return self._in_module(candidate, parts[cut:], depth)
        return ("unresolved", None)

    def _in_module(self, name: str, rest: list[str], depth: int) -> _Resolution:
        if not rest:
            return ("bound", None)
        module = self.modules[name]
        head, tail = rest[0], ".".join(rest[1:])
        if head in module.defs and not tail:
            return ("edge", f"{name}.{head}")
        if head in module.classes:
            return ("edge", self._in_class(module, head, tail))
        if head in module.imports:
            target = module.imports[head] + (f".{tail}" if tail else "")
            return self.classify_global(target, depth + 1)
        if head in module.bound or head in module.defs:
            return ("bound", None)
        return ("unresolved", None)


def permission_gated(graph: Graph) -> dict[str, str]:
    """Return every function that transitively reaches a decision, with why."""
    callers: dict[str, set[str]] = {}
    for source, targets in graph.edges.items():
        for target in targets:
            callers.setdefault(target, set()).add(source)
    gated = dict(graph.seeds)
    frontier = list(graph.seeds)
    while frontier:
        current = frontier.pop()
        for caller in callers.get(current, ()):
            if caller not in gated:
                gated[caller] = f"reaches {current}"
                frontier.append(caller)
    return gated


def reachable(graph: Graph, symbol: str) -> set[str]:
    """Return every function transitively referenced from ``symbol``."""
    seen = {symbol}
    frontier = [symbol]
    while frontier:
        for target in graph.edges.get(frontier.pop(), ()):
            if target not in seen:
                seen.add(target)
                frontier.append(target)
    return seen


def unreviewed_dynamic_dispatch(graph: Graph, symbol: str) -> list[str]:
    """Return reachable dynamic dispatch the census cannot follow."""
    if symbol in DYNAMIC_DISPATCH_REVIEWED:
        return []
    return sorted(
        f"{target}: {graph.dynamic[target]}"
        for target in reachable(graph, symbol)
        if target in graph.dynamic
    )


def spelled_gates(graph: Graph) -> set[str]:
    """Cross-check only: functions whose own body spells a known gate."""
    return {
        f"{module.name}.{qualname}"
        for module in graph.modules.values()
        for qualname, node in module.defs.items()
        if _SPELLED_GATE.search(ast.unparse(node))
    }
