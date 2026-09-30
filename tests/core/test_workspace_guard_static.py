"""Fail-closed static scan: no test reaches into or rewrites the refusal guard.

The runtime observer (tests/workspace_refusal_observer.py) catches fixture
level disablement by content; in-process code can still rewrite anything, so
this scan rejects the plainly written forms of that residual. Outside the
guard's own modules, a test must not reference the observer's stash keys,
handler chain or internals, must not rewrite function state (``__code__``,
``__globals__``, ``__defaults__``, ``__kwdefaults__``, ``__closure__`` cells,
``__dict__``), and must not patch the guard modules or ``request_started``. A
mutation primitive whose target cannot be resolved statically fails too.
Deliberately aliased or introspective chains (names built at run time, aliases
of the objects, reflection) are outside what a syntactic scan can see, and
outside the runtime observer's boundary: only code review covers them.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

import pytest

SELF = Path(__file__).resolve()
TESTS = SELF.parents[1]
# The guard's own modules; the scan does not apply to them.
GUARD_MODULES = frozenset(
    {
        "workspace_refusal_branches",
        "workspace_refusal_observer",
        "workspace_refusal_support",
    }
)
# Route discovery is shared with the route-derived refusal tests.
PUBLIC = {
    "workspace_refusal_support": frozenset({"WorkspaceRoute", "workspace_routes"})
}
FUNCTION_STATE = frozenset(
    {
        "__closure__",
        "__code__",
        "__defaults__",
        "__dict__",
        "__globals__",
        "__kwdefaults__",
        "cell_contents",
    }
)
# Readable in place of a mutable namespace or cell; there is no test use.
UNREADABLE = frozenset({"__globals__", "__closure__", "cell_contents"})
# Handler chain and stash storage reach the observer without naming it; the
# signal carries the observer's independent request count.
REACH = frozenset({"_middleware_chain", "_storage", "request_started"})
GRAPH_WALKS = frozenset({"get_objects", "get_referents", "get_referrers"})
UNSAFE_MODULES = frozenset({"ctypes"})
MAPPING_WRITES = frozenset(
    {"__delitem__", "__setitem__", "clear", "pop", "popitem", "setdefault", "update"}
)
_CONFTEST_KEYS = (
    ("GUARD_STATS", 0, 3),
    ("REFUSAL_BEFORE", 1, 2),
    ("REFUSAL_COUNTS", 1, 2),
    ("REFUSAL_FAILED", 1, 3),
    ("REFUSAL_INTEGRITY", 1, 2),
    ("REFUSAL_ISSUES", 1, 3),
    ("REFUSAL_OBSERVER", 1, 6),
    ("REFUSAL_RECEIPTS", 1, 3),
    ("REFUSAL_SEAL", 1, 2),
    ("REFUSAL_SESSION_ERRORS", 1, 2),
    ("TAMPERING_BEFORE", 1, 2),
)
_INTEGRITY = "core/test_workspace_guard_integrity.py"
# Exact findings allowed, each with its reason. A new finding, or a count that
# no longer matches, fails the scan.
ALLOWED = Counter(
    {
        # conftest installs the observer and defines the keys it is held under.
        **{
            ("conftest.py", f"{verb} {key}"): count
            for key, defined, referenced in _CONFTEST_KEYS
            for verb, count in (("defines", defined), ("references", referenced))
            if count
        },
        (
            "conftest.py",
            "imports guard internal workspace_refusal_observer.RefusalObserver",
        ): 1,
        (
            "conftest.py",
            "imports guard internal workspace_refusal_support.GUARD_STATS",
        ): 1,
        # The exit gate reads the observer's trace after the corpus.
        ("core/test_workspace_refusal_exits.py", "references REFUSAL_OBSERVER"): 2,
        # Unit tests of the check itself, outside the collection observer.
        (
            "core/test_workspace_refusal_guard.py",
            "imports guard internal workspace_refusal_support.RefusalGuardStats",
        ): 1,
        (
            "core/test_workspace_refusal_guard.py",
            "imports guard internal workspace_refusal_support.check_refusal",
        ): 1,
        # Subprocess tamper proofs: plant bypasses, assert the guard fails.
        (
            _INTEGRITY,
            "imports guard internal workspace_refusal_observer.RefusalMiddleware",
        ): 1,
        (_INTEGRITY, "names guard module workspace_refusal_observer"): 1,
        (_INTEGRITY, "names guard module workspace_refusal_support"): 2,
        # Sets OTP state on an unsaved synthetic User (no model attributes).
        ("auth/test_stepup.py", "mutates __dict__"): 2,
        # Seed a freshly loaded synthetic guard module's globals (todo 6 census).
        ("identity/test_guard_classification.py", "mutates __dict__"): 1,
        ("identity/test_nonstaff_differential.py", "mutates __dict__"): 1,
        # Records every current_context function it wraps (todo 6 census).
        ("identity/authority_observer.py", "setattr with dynamic name"): 1,
        # Patch a module attribute named by the test parameter or module name.
        ("core/test_workspace_late_refusals.py", "setattr with dynamic name"): 2,
        ("renewal/test_runtime_paths.py", "setattr with dynamic name"): 1,
        ("renewal/test_browser_runner.py", "setattr with dynamic name"): 1,
        ("renewal/browser/test_workspace.py", "setattr with dynamic target"): 1,
    }
)


def _stash_keys(path: Path) -> frozenset[str]:
    """Module-level ``pytest.StashKey`` names: the handles to guard state."""
    tree = ast.parse(path.read_text(), filename=str(path))
    return frozenset(
        node.target.id
        for node in tree.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and "StashKey" in ast.unparse(node.annotation)
    )


def _string_constants(path: Path) -> frozenset[str]:
    """Module-level string constants, e.g. the response attribute naming it."""
    tree = ast.parse(path.read_text(), filename=str(path))
    return frozenset(
        node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def forbidden_names() -> frozenset[str]:
    """Derived from the guard's modules and conftest, plus the reach names."""
    sources = [TESTS / "conftest.py", *(TESTS / f"{m}.py" for m in GUARD_MODULES)]
    derived = frozenset().union(
        *(_stash_keys(path) for path in sources),
        *(_string_constants(TESTS / f"{m}.py") for m in GUARD_MODULES),
    )
    assert {"REFUSAL_OBSERVER", "GUARD_STATS"} <= derived
    return derived | REACH


def _dotted(node: ast.expr) -> str:
    return ast.unparse(node)


def _is_state_mapping(node: ast.expr) -> bool:
    """``x.__dict__``, ``x.__globals__`` or ``vars(x)``."""
    if isinstance(node, ast.Attribute):
        return node.attr in {"__dict__", "__globals__"}
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "vars"
    )


class _Scan(ast.NodeVisitor):
    def __init__(self, forbidden: frozenset[str]) -> None:
        self.forbidden = forbidden
        self.findings: list[tuple[int, str]] = []

    def found(self, node: ast.AST, message: str) -> None:
        self.findings.append((getattr(node, "lineno", 0), message))

    def _name(self, node: ast.AST, name: str) -> None:
        if name in self.forbidden:
            self.found(node, f"references {name}")
        if name.split(".", 1)[0] in GUARD_MODULES:
            self.found(node, f"names guard module {name}")
        if name.split(".", 1)[0] in UNSAFE_MODULES:
            self.found(node, f"uses {name}")

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._name(node, alias.name)
            if alias.asname:
                self._name(node, alias.asname)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        if module in UNSAFE_MODULES:
            self.found(node, f"uses {module}")
        for alias in node.names:
            if module in GUARD_MODULES:
                if alias.name not in PUBLIC.get(module, frozenset()):
                    self.found(node, f"imports guard internal {module}.{alias.name}")
            else:
                self._name(node, alias.name)
            if alias.asname:
                self._name(node, alias.asname)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Store) and node.id in self.forbidden:
            self.found(node, f"defines {node.id}")
        else:
            self._name(node, node.id)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if isinstance(node.target, ast.Name) and node.target.id in self.forbidden:
            # Counted once as a definition, not also as a reference.
            self.found(node, f"defines {node.target.id}")
            if node.value is not None:
                self.visit(node.value)
            self.visit(node.annotation)
            return
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr in self.forbidden:
            self.found(node, f"references {node.attr}")
        if node.attr in FUNCTION_STATE and isinstance(node.ctx, (ast.Store, ast.Del)):
            self.found(node, f"assigns {node.attr}")
        elif node.attr in UNREADABLE:
            self.found(node, f"reads {node.attr}")
        if node.attr in GRAPH_WALKS:
            self.found(node, f"walks the object graph ({node.attr})")
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)) and _is_state_mapping(node.value):
            self.found(node, f"mutates {_state_label(node.value)}")
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str):
            self._name(node, node.value)

    def visit_Call(self, node: ast.Call) -> None:
        function = node.func
        name = (
            function.id
            if isinstance(function, ast.Name)
            else function.attr
            if isinstance(function, ast.Attribute)
            else ""
        )
        receiver = (
            _dotted(function.value) if isinstance(function, ast.Attribute) else ""
        )
        mock = receiver.split(".")[-1] in {"mock", "patch"}
        if name in {"__setattr__", "__delattr__"}:
            self.found(node, f"calls {name}")
        elif name in {"setattr", "delattr"}:
            self._setattr(node, dotted_form=bool(receiver))
        elif name == "object" and receiver.split(".")[-1] == "patch":
            self._setattr(node, dotted_form=False)
        elif (name == "patch" and (not receiver or mock)) or (
            name == "dict" and receiver.split(".")[-1] == "patch"
        ):
            self._patch(node, mapping=name == "dict")
        elif name in {"setitem", "delitem"}:
            if node.args and _is_state_mapping(node.args[0]):
                self.found(node, f"mutates {_state_label(node.args[0])}")
        elif (
            name in MAPPING_WRITES
            and isinstance(function, ast.Attribute)
            and _is_state_mapping(function.value)
        ):
            self.found(node, f"mutates {_state_label(function.value)}")
        self.generic_visit(node)

    def _setattr(self, node: ast.Call, *, dotted_form: bool) -> None:
        first = node.args[0] if node.args else _keyword(node, "target")
        if dotted_form and first is not None and len(node.args) <= 2:
            # The two-argument form names its target as a dotted string.
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                self._target(node, first.value)
                return
            if _maybe_string(first):
                self.found(node, "setattr with dynamic target")
                return
        attribute = _keyword(node, "name") or (
            node.args[1] if len(node.args) > 1 else None
        )
        if isinstance(attribute, ast.Constant) and isinstance(attribute.value, str):
            self._target(node, attribute.value)
        else:
            self.found(node, "setattr with dynamic name")

    def _patch(self, node: ast.Call, *, mapping: bool) -> None:
        first = node.args[0] if node.args else _keyword(node, "target")
        if mapping:
            if first is not None and _is_state_mapping(first):
                self.found(node, f"mutates {_state_label(first)}")
            return
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            self._target(node, first.value)
        elif first is not None:
            self.found(node, "patch with dynamic target")

    def _target(self, node: ast.AST, dotted: str) -> None:
        parts = dotted.split(".")
        for part in parts:
            if part in FUNCTION_STATE:
                self.found(node, f"assigns {part}")
        # Names inside the string are reported by visit_Constant.


def _keyword(node: ast.Call, name: str) -> ast.expr | None:
    return next((k.value for k in node.keywords if k.arg == name), None)


def _maybe_string(node: ast.expr) -> bool:
    return isinstance(node, (ast.JoinedStr, ast.BinOp)) or (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"format", "join"}
    )


def _state_label(node: ast.expr) -> str:
    return node.attr if isinstance(node, ast.Attribute) else "vars()"


def scan(source: str, filename: str, forbidden: frozenset[str]) -> list[str]:
    """Findings for one file; an unparsable file is itself a finding."""
    try:
        tree = ast.parse(source, filename=filename)
    except (SyntaxError, ValueError) as error:
        return [f"{filename}:0: unparsable ({type(error).__name__})"]
    visitor = _Scan(forbidden)
    visitor.visit(tree)
    return [f"{filename}:{line}: {message}" for line, message in visitor.findings]


def tree_findings() -> Counter[tuple[str, str]]:
    forbidden = forbidden_names()
    counts: Counter[tuple[str, str]] = Counter()
    paths = sorted(TESTS.rglob("*.py"))
    assert len(paths) > 100
    for path in paths:
        relative = path.relative_to(TESTS).as_posix()
        # The guard's own modules, and this scan with its vocabulary.
        if (path.stem in GUARD_MODULES and path.parent == TESTS) or path == SELF:
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            counts[relative, f"unreadable ({type(error).__name__})"] += 1
            continue
        for finding in scan(source, relative, forbidden):
            counts[relative, finding.split(": ", 1)[1]] += 1
    return counts


def test_no_test_reaches_or_rewrites_the_refusal_guard() -> None:
    findings = tree_findings()
    unexpected = findings - ALLOWED
    stale = ALLOWED - findings
    assert not unexpected, "Refusal guard tampering constructs:\n" + "\n".join(
        f"tests/{path}: {message} (x{count})"
        for (path, message), count in sorted(unexpected.items())
    )
    # An allowance that no longer matches must be removed, not kept as slack.
    assert not stale, f"Stale refusal guard allowances: {sorted(stale)}"


# Each is planted in an ordinary test module; each must be a finding.
MUTANTS = {
    # Round-8 reviewer one-liners, the observer reached through a fixture.
    "frozen-check-code": "observer.check.__code__ = noop.__code__",
    "frozen-scope-code": (
        "observer.check.__globals__['workspace_scope'].__code__ = never.__code__"
    ),
    "frozen-namespace": "observer.check.__globals__['workspace_scope'] = never",
    # The same, spelled through the mutation helpers.
    "monkeypatch-code": "monkeypatch.setattr(check, '__code__', noop.__code__)",
    "setattr-code": "setattr(check, '__code__', noop.__code__)",
    "object-setattr": "object.__setattr__(check, '__defaults__', ())",
    "setitem-globals": "monkeypatch.setitem(check.__globals__, 'x', never)",
    "update-globals": "check.__globals__.update(workspace_scope=never)",
    "cell": "check.__closure__[0].cell_contents = never",
    "kwdefaults": "check.__kwdefaults__ = {}",
    "dict-item": "observer.__dict__['check'] = noop",
    "vars-item": "vars(observer)['check'] = noop",
    "patch-dict": "patch.dict(observer.__dict__, check=noop)",
    "dynamic-name": "monkeypatch.setattr(check, name, noop)",
    "dynamic-target": "monkeypatch.setattr(f'{module}.check_refusal', noop)",
    # Reaching the observer or its modules.
    "stash-key": (
        "from conftest import REFUSAL_OBSERVER\n"
        "observer = request.config.stash[REFUSAL_OBSERVER]"
    ),
    "conftest-attribute": "import conftest\nkey = conftest.REFUSAL_OBSERVER",
    "handler-chain": "chain = client.handler._middleware_chain",
    "handler-chain-string": "chain = getattr(client.handler, '_middleware_chain')",
    "response-attribute": (
        "observer = getattr(response, '_workspace_refusal_observation')"
    ),
    "stash-storage": "items = request.config.stash._storage",
    "gc": "import gc\nobjects = gc.get_objects()",
    "ctypes": "import ctypes",
    "module-import": "import workspace_refusal_observer",
    "internal-import": "from workspace_refusal_support import check_refusal",
    "string-module": "module = sys.modules['workspace_refusal_support']",
    "string-target": (
        "monkeypatch.setattr('workspace_refusal_support.workspace_scope', never)"
    ),
    "mock-target": "patch('workspace_refusal_observer.check_refusal', noop)",
    "signal-setattr": (
        "from django.core import signals\n"
        "monkeypatch.setattr(signals.request_started, 'receivers', [])"
    ),
    "signal-import": "from django.core.signals import request_started",
    "unparsable": "def broken(:",
}


@pytest.mark.parametrize("mutant", sorted(MUTANTS))
def test_each_planted_tampering_construct_is_found(mutant: str) -> None:
    body = "\n".join(f"    {line}" for line in MUTANTS[mutant].splitlines())
    source = f"def test_planted(observer, check, monkeypatch):\n{body}\n"
    assert scan(source, "core/test_planted.py", forbidden_names())


def test_public_route_discovery_and_ordinary_patching_are_not_findings() -> None:
    source = (
        "from workspace_refusal_support import WorkspaceRoute, workspace_routes\n"
        "def test_ok(monkeypatch, user):\n"
        "    monkeypatch.setattr(user, 'is_active', False)\n"
        "    monkeypatch.setattr('apps.core.workspace.utc_now', lambda: 0)\n"
        "    code = user.save.__code__\n"
    )
    assert scan(source, "core/test_ok.py", forbidden_names()) == []
