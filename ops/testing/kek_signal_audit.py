"""Audit source derived from the KEK lane's Git history, including its test drivers."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from .pidfd_command import Command, run_bytes

BASE = "4f8d2454987ca1d083ead53a7501126d647a89bc"
_NUMERIC = frozenset({"os.kill", "os.killpg", "signal.pthread_kill"})
_METHODS = frozenset({"kill", "terminate", "send_signal"})
_GETATTR_ARGS = 2
_TIMED = frozenset(
    {
        "subprocess.run",
        "subprocess.call",
        "subprocess.check_call",
        "subprocess.check_output",
    }
)
_SHELL = re.compile(
    r"(?:^|[;&|(\s])(?:exec\s+|command\s+)?(?:/[\w/.-]+/)?(?:kill|pkill|killall)(?:\s|$)"
)


@dataclass(frozen=True, order=True, slots=True)
class Finding:
    """A path, not source text or runtime arguments, identifies a violation."""

    path: str
    line: int
    rule: str


def _literal(node: ast.expr) -> str:
    return (
        node.value
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        else ""
    )


def _name(node: ast.expr, names: dict[str, str]) -> str:
    if isinstance(node, ast.Name):
        return names.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        return _name(node.value, names) + "." + node.attr
    if isinstance(node, ast.Call):
        fn = _name(node.func, names)
        if fn in {"getattr", "builtins.getattr"} and len(node.args) >= _GETATTR_ARGS:
            return _name(node.args[0], names) + "." + _literal(node.args[1])
        return fn + "()"
    return ""


def _numeric_method(
    name: str, receivers: dict[str, str], local_methods: set[str]
) -> bool:
    obj, _, method = name.rpartition(".")
    if method not in _METHODS:
        return False
    # Resolve a repository-defined, typed receiver to its own audited method.
    # An unknown receiver is not evidence that a call is safe.
    return receivers.get(obj, "") + "." + method not in local_methods


def _aliases(tree: ast.AST) -> dict[str, str]:
    names: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update({x.asname or x.name: x.name for x in node.names})
        if isinstance(node, ast.ImportFrom):
            names.update(
                {x.asname or x.name: f"{node.module}.{x.name}" for x in node.names}
            )
    assignments = [node for node in ast.walk(tree) if isinstance(node, ast.Assign)]
    for _ in range(len(assignments) + 1):
        changed = False
        for assignment in assignments:
            target = _name(assignment.value, names)
            if (
                target not in _NUMERIC | _TIMED
                and target.rsplit(".", 1)[-1] not in _METHODS
            ):
                continue
            for dest in assignment.targets:
                if isinstance(dest, ast.Name) and names.get(dest.id) != target:
                    names[dest.id] = target
                    changed = True
        if not changed:
            break
    return names


def _unsafe_timeout(node: ast.Call) -> bool:
    return any(
        kw.arg is None
        or (
            kw.arg == "timeout"
            and not (isinstance(kw.value, ast.Constant) and kw.value.value is None)
        )
        for kw in node.keywords
    )


def _receiver_facts(
    path: str,
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    names: dict[str, str],
) -> dict[str, str]:
    args = (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
    types = {a.arg: {_name(a.annotation, names)} for a in args if a.annotation}
    module = path.removesuffix(".py").replace("/", ".")
    for stmt in ast.walk(node):
        if not isinstance(stmt, ast.Assign):
            continue
        call = stmt.value
        if isinstance(call, ast.Call):
            cls = _name(call.func, names)
            cls = cls if "." in cls else module + "." + cls
        elif isinstance(call, ast.Constant) and call.value is None:
            continue
        else:
            cls = ""
        for var in stmt.targets:
            if isinstance(var, ast.Name):
                types.setdefault(var.id, set()).add(cls)
    return {k: next(iter(v)) for k, v in types.items() if len(v) == 1}


def _shell_signal(node: ast.Call, fn: str) -> bool:
    if not node.args or fn not in _TIMED | {"subprocess.Popen", "os.system"}:
        return False
    first = node.args[0]
    arg = (
        first.elts[0]
        if isinstance(first, (ast.List, ast.Tuple)) and first.elts
        else first
    )
    if Path(_literal(arg)).name in {"kill", "pkill", "killall"}:
        return True
    is_shell = fn == "os.system" or any(
        kw.arg == "shell"
        and isinstance(kw.value, ast.Constant)
        and kw.value.value is True
        for kw in node.keywords
    )
    return is_shell and bool(_SHELL.search(_literal(first)))


def python_findings(
    path: str, source: str, declared: set[str] | None = None
) -> list[Finding]:
    """Resolve aliases and reject numeric signal and implicit timeout routes."""
    tree = ast.parse(source, filename=path)
    names = _aliases(tree)
    methods = declared or set()
    violations: set[Finding] = set()

    def visit(node: ast.AST, receivers: dict[str, str]) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            receivers = _receiver_facts(path, node, names)
        if isinstance(node, (ast.Attribute, ast.Name)):
            ref = _name(node, names)
            if ref in _NUMERIC or _numeric_method(ref, receivers, methods):
                violations.add(Finding(path, node.lineno, "numeric-signal"))
        if isinstance(node, ast.Call):
            fn = _name(node.func, names)
            if fn in _NUMERIC or _numeric_method(fn, receivers, methods):
                violations.add(Finding(path, node.lineno, "numeric-signal"))
            if fn in _TIMED and _unsafe_timeout(node):
                violations.add(Finding(path, node.lineno, "implicit-timeout-signal"))
            if _shell_signal(node, fn):
                violations.add(Finding(path, node.lineno, "signal-command"))
        for child in ast.iter_child_nodes(node):
            visit(child, receivers)

    visit(tree, {})
    return sorted(violations)


def _git(repo: Path, *args: str) -> bytes:
    result = run_bytes(("/usr/bin/git", "-C", str(repo), *args), Command(30))
    result.check_returncode()
    return result.stdout


def _paths(data: bytes) -> set[str]:
    return {os.fsdecode(p) for p in data.split(b"\0") if p}


def _source_kind(name: str, first_line: bytes) -> str:
    path = Path(name)
    shebang = (
        first_line.removeprefix(b"#!").split() if first_line.startswith(b"#!") else []
    )
    interpreters = {os.fsencode(Path(os.fsdecode(word)).name) for word in shebang}
    if path.suffix == ".py" or any(
        re.fullmatch(rb"python(?:\d+(?:\.\d+)*)?", word) for word in interpreters
    ):
        return "python"
    if (
        path.suffix in {".sh", ".bash", ".mk"}
        or path.name in {"Makefile", "GNUmakefile"}
        or path.name.startswith("Makefile.")
        or interpreters & {b"sh", b"bash", b"dash", b"ksh"}
    ):
        return "shell"
    return ""


def scope(repo: Path, revision: str | None = None) -> dict[str, str]:
    """Use all changed paths since the lane base; no hand-list of entry points."""
    head = revision or "HEAD"
    if revision is not None and not re.fullmatch("[0-9a-f]{40}", revision):
        message = "require an exact product commit"
        raise ValueError(message)
    _git(repo, "merge-base", "--is-ancestor", BASE, head)
    names = _paths(
        _git(
            repo,
            "log",
            "--format=",
            "--name-only",
            "-z",
            "--no-renames",
            BASE + ".." + head,
        )
    )
    if revision is None:
        names |= _paths(_git(repo, "diff", "--name-only", "-z", "--no-renames", BASE))
        names |= _paths(_git(repo, "ls-files", "-z", "--others", "--exclude-standard"))
        existing = _paths(
            _git(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
        )
    else:
        existing = _paths(_git(repo, "ls-tree", "-r", "-z", "--name-only", head))
    sources: dict[str, str] = {}
    for name in sorted(names & existing):
        path = repo / name
        if revision is None and (not path.is_file() or path.is_symlink()):
            continue
        raw = (
            path.read_bytes()
            if revision is None
            else _git(repo, "cat-file", "blob", head + ":" + name)
        )
        if _source_kind(name, raw.partition(b"\n")[0]):
            sources[name] = raw.decode("utf-8")
    return sources


def audit(sources: dict[str, str]) -> list[Finding]:
    """Resolve local classes from the scanned sources themselves."""
    methods: set[str] = set()
    for name, src in sources.items():
        if _source_kind(name, src.partition("\n")[0].encode()) == "python":
            module = name[:-3].replace("/", ".")
            methods.update(
                f"{module}.{cls.name}.{fn.name}"
                for cls in ast.parse(src).body
                if isinstance(cls, ast.ClassDef) and not cls.bases
                for fn in cls.body
                if isinstance(fn, ast.FunctionDef)
            )
    result: list[Finding] = []
    for name, src in sources.items():
        if _source_kind(name, src.partition("\n")[0].encode()) == "python":
            result.extend(python_findings(name, src, methods))
        else:
            result.extend(
                Finding(name, line, "signal-command")
                for line, text in enumerate(src.splitlines(), 1)
                if not text.lstrip().startswith("#") and _SHELL.search(text)
            )
    return sorted(result)


def main() -> int:
    """Emit path/blob evidence for the dynamically discovered source set."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision")
    args = parser.parse_args()
    sources = scope(Path.cwd(), args.revision)
    if not sources:
        message = "empty signal audit scope"
        raise ValueError(message)
    findings = audit(sources)
    sys.stdout.write(
        json.dumps(
            {
                "revision": args.revision,
                "base": BASE,
                "files": {
                    k: hashlib.sha256(v.encode()).hexdigest()
                    for k, v in sources.items()
                },
                "findings": [asdict(f) for f in findings],
            },
            sort_keys=True,
        )
        + "\n"
    )
    return int(bool(findings))


if __name__ == "__main__":
    raise SystemExit(main())
