"""Derive the permission-gated runtime functions from the live system.

A census row may carry an exemption label (infrastructure, provider, ...)
only if the function cannot reach a permission decision. "Reaches" is not a
spelling list: the permission decisions are read from the live database
(``clinic_app.has_permission`` and every SQL function whose body reaches it,
plus the tables whose RLS policies call one), and the Python side is the
transitive closure of every function that references a function which
executes one of those decisions or touches one of those tables. References
resolve through imports, aliases, module attributes, ``self``/``cls``
methods, decorators and functions passed as values, so wrapping a gate in a
helper, a helper chain or an alias does not hide it.

Anything the census cannot follow fails closed: an opaque SQL function that
is not extension-owned is an error, and an exempted row whose reachable code
uses dynamic dispatch (``getattr`` with a computed name, ``import_module``,
``eval``...) needs an explicit reviewed ``DYNAMIC_DISPATCH_REVIEWED`` entry.
"""

from __future__ import annotations

import ast
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


@dataclass(slots=True)
class _Module:
    name: str
    defs: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = field(
        default_factory=dict
    )
    classes: dict[str, set[str]] = field(default_factory=dict)
    imports: dict[str, str] = field(default_factory=dict)
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
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        module.defs[node.name] = node
    elif isinstance(node, ast.ClassDef):
        methods: set[str] = set()
        for item in node.body:
            if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef):
                module.defs[f"{node.name}.{item.name}"] = item
                methods.add(item.name)
        module.classes[node.name] = methods
        table = _db_table(node)
        if table:
            module.tables[node.name] = table
    elif isinstance(node, ast.Import | ast.ImportFrom):
        _collect_import(module, node)
    elif isinstance(node, ast.If):
        # TYPE_CHECKING / version branches still bind names.
        for child in (*node.body, *node.orelse):
            _collect_node(module, child)


def _collect_import(module: _Module, node: ast.Import | ast.ImportFrom) -> None:
    if isinstance(node, ast.Import):
        for alias in node.names:
            bound = alias.asname or alias.name.split(".", 1)[0]
            module.imports[bound] = alias.name if alias.asname else bound
        return
    if node.level:
        message = f"relative import in {module.name} is not resolved"
        raise CensusError(message)
    for alias in node.names:
        module.imports[alias.asname or alias.name] = f"{node.module}.{alias.name}"


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


def _app_label(module: str) -> str:
    parts = module.split(".")
    return parts[1] if len(parts) > 1 and parts[0] == "apps" else ""


def build_graph(
    decisions: LiveDecisions, sources: Mapping[str, str] | None = None
) -> Graph:
    """Parse ``apps`` (``sources`` overrides file text by module name)."""
    modules: dict[str, _Module] = {}
    trees: dict[str, ast.Module] = {}
    for path in sorted((ROOT / "apps").rglob("*.py")):
        if "migrations" in path.parts:
            continue
        name = _module_name(path)
        text = (sources or {}).get(name)
        tree = ast.parse(path.read_text() if text is None else text)
        trees[name] = tree
        modules[name] = _collect(name, tree)
    graph = Graph(modules, {}, {}, {})
    resolver = _Resolver(modules)
    tables = _model_tables(modules)
    for module in modules.values():
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
    targets: set[str] = set()
    for dotted in _references(node):
        target = resolver.resolve(module, dotted, owner)
        if target is None:
            continue
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


class _Resolver:
    def __init__(self, modules: Mapping[str, _Module]) -> None:
        self.modules = modules

    def resolve(self, module: _Module, dotted: str, owner: str) -> str | None:
        head, _, rest = dotted.partition(".")
        if head in ("self", "cls"):
            return self._own_method(module, owner, rest)
        if head in module.defs and not rest:
            return f"{module.name}.{head}"
        if head in module.classes:
            return self._in_class(module, head, rest)
        if head in module.imports:
            target = module.imports[head] + (f".{rest}" if rest else "")
            return self.resolve_global(target, depth=0)
        return None

    @staticmethod
    def _own_method(module: _Module, owner: str, rest: str) -> str | None:
        method = rest.split(".", 1)[0]
        if owner and method in module.classes.get(owner, set()):
            return f"{module.name}.{owner}.{method}"
        return None

    def _in_class(self, module: _Module, cls: str, rest: str) -> str:
        method = rest.split(".", 1)[0] if rest else ""
        if method in module.classes[cls]:
            return f"{module.name}.{cls}.{method}"
        return f"{module.name}.{cls}"

    def resolve_global(self, dotted: str, depth: int) -> str | None:
        """Resolve an absolute dotted name, following re-exports."""
        if depth > _MAX_REEXPORT_DEPTH:
            return None
        parts = dotted.split(".")
        for cut in range(len(parts), 0, -1):
            candidate = ".".join(parts[:cut])
            if candidate in self.modules:
                return self._in_module(candidate, ".".join(parts[cut:]), depth)
        return None

    def _in_module(self, name: str, rest: str, depth: int) -> str | None:
        module = self.modules[name]
        head, _, tail = rest.partition(".")
        if not head:
            return None
        if head in module.defs and not tail:
            return f"{name}.{head}"
        if head in module.classes:
            return self._in_class(module, head, tail)
        if head in module.imports:
            target = module.imports[head] + (f".{tail}" if tail else "")
            return self.resolve_global(target, depth + 1)
        return None


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
