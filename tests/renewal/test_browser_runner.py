from __future__ import annotations

import ast
import contextlib
import functools
import inspect
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from ops.testing import renewal_runner as runner
from ops.testing.browser_server_supervisor import SupervisedMaster
from ops.testing.isolation_common import IsolationError, JsonObject
from playwright.sync_api import (
    ElementHandle,
    Frame,
    Keyboard,
    Locator,
    Page,
    sync_playwright,
)

from renewal.browser import _fixture_secrets as fixture_secrets
from renewal.browser import a11y_support, engines

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

REPOSITORY = Path(__file__).resolve().parents[2]


def _private(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True)
    return path


def test_suite_registry_is_closed_and_registered_files_exist() -> None:
    assert set(runner.SUITES) == {
        "agenda",
        "realtime",
        "amendments",
        "attachments",
        "availability",
        "billing",
        "clinic-settings",
        "contacts",
        "consent",
        "encounter",
        "end-to-end",
        "clinical-history",
        "clinician-video",
        "video-recovery",
        "locale",
        "patient-access",
        "patient-video",
        "questionnaires",
        "retention",
        "self-booking",
        "waitlist",
        "reminders",
        "teleconsult",
        "primitives",
        "prescription-draft",
        "prescribing",
        "document-verification",
        "smoke",
        "staff-intake",
        "workspace",
    }
    assert "prescription-draft" in runner.FIXTURE_SUITES
    assert runner.SIGNING_SUITES <= runner.FIXTURE_SUITES
    assert "--cov=apps.prescription" in runner.COVERAGE_TARGETS
    assert set(runner.SUITES) >= runner.FIXTURE_SUITES
    assert "video-recovery" in runner.FIXTURE_SUITES & runner.VIDEO_SUITES
    # The rehearsal drives every synthetic adapter slice in one runtime.
    assert "end-to-end" in (
        runner.FIXTURE_SUITES
        & runner.VIDEO_SUITES
        & runner.SIGNING_SUITES
        & runner.PAYMENT_SUITES
    )
    for suite in runner.SUITES.values():
        for relpath in suite:
            assert (REPOSITORY / relpath).is_file()
            assert relpath.startswith("tests/renewal/browser/")


def test_coverage_targets_include_the_teleconsult_domain() -> None:
    assert "--cov=apps.teleconsult" in runner.COVERAGE_TARGETS


def test_ci_gate_names_are_the_plan_gates() -> None:
    assert runner.CI_GATES == (
        "static",
        "migration",
        "coverage",
        "dependency",
        "image-tls",
        "browser",
    )


def test_main_rejects_invalid_grammar() -> None:
    assert runner.main([]) == 2
    assert runner.main(["bogus"]) == 2
    assert runner.main(["browser"]) == 2
    assert runner.main(["browser", "--suite"]) == 2
    assert runner.main(["browser", "--suite", "smoke", "--bogus", "x"]) == 2
    assert runner.main(["ci", "--suite", "smoke"]) == 2


def test_main_rejects_unknown_suite(tmp_path: Path) -> None:
    assert (
        runner.main(
            [
                "browser",
                "--suite",
                "runtime-https",
                "--artifact-root",
                str(_private(tmp_path / "artifacts")),
            ]
        )
        == 2
    )


def test_serving_dsn_requires_exactly_the_app_role() -> None:
    good = "postgresql://clinic_app:pw@127.0.0.1:55432/clinic"
    assert runner._validate_serving_dsn(good) == good
    for bad in (
        "postgresql://clinic_owner:pw@127.0.0.1:55432/clinic",
        "postgresql://postgres:pw@127.0.0.1:55432/clinic",
        "postgresql://clinic_super:pw@127.0.0.1:55432/clinic",
        "postgresql://clinic_resolver:pw@127.0.0.1:55432/clinic",
        "postgresql://clinic_app:pw@db.internal:55432/clinic",
        "postgresql://clinic_app:pw@10.0.0.4:55432/clinic",
        "postgresql://clinic_app@127.0.0.1:55432/clinic",
        "postgresql://clinic_app:pw@127.0.0.1:55432/",
        "postgresql://clinic_app:pw@127.0.0.1/clinic",
        "postgres://clinic_app:pw@127.0.0.1:55432/clinic",
        "postgresql://clinic_app:pw@127.0.0.1:5432/clinic",
        "not-a-dsn",
        "",
    ):
        with pytest.raises(IsolationError):
            runner._validate_serving_dsn(bad)


def test_owner_dsn_override_is_rejected_before_any_provisioning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "CLINIC_RENEWAL_APP_DATABASE_URL",
        "postgresql://clinic_owner:pw@127.0.0.1:55432/clinic",
    )

    def _explode(*args: object, **kwargs: object) -> None:
        message = "docker must never run for a rejected DSN"
        raise AssertionError(message)

    monkeypatch.setattr(runner, "_docker", _explode)
    assert (
        runner.main(
            [
                "browser",
                "--suite",
                "smoke",
                "--artifact-root",
                str(tmp_path / "artifacts"),
            ]
        )
        == 2
    )


def test_missing_browser_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLINIC_RENEWAL_BROWSER_EXECUTABLE", "/nonexistent/chrome")
    assert (
        runner.main(
            [
                "browser",
                "--suite",
                "smoke",
                "--artifact-root",
                str(tmp_path / "artifacts"),
            ]
        )
        == 2
    )


def test_browser_resolution_honors_the_private_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "chrome"
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o755)
    monkeypatch.setenv("CLINIC_RENEWAL_BROWSER_EXECUTABLE", str(executable))
    assert runner._resolve_browser() == str(executable)
    monkeypatch.setenv("CLINIC_RENEWAL_BROWSER_EXECUTABLE", str(tmp_path / "gone"))
    with pytest.raises(IsolationError, match="browser"):
        runner._resolve_browser()
    monkeypatch.delenv("CLINIC_RENEWAL_BROWSER_EXECUTABLE")
    monkeypatch.setattr("ops.testing.renewal_runner.shutil.which", lambda _name: None)
    with pytest.raises(IsolationError, match="browser"):
        runner._resolve_browser()


def test_engine_selection_defaults_to_chromium_and_rejects_the_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CLINIC_BROWSER_ENGINE", raising=False)
    assert runner.ENGINES == ("chromium", "firefox", "webkit")
    assert runner._resolve_engine(None) == "chromium"
    assert runner._resolve_engine("webkit") == "webkit"
    monkeypatch.setenv("CLINIC_BROWSER_ENGINE", "firefox")
    assert runner._resolve_engine(None) == "firefox"
    assert runner._resolve_engine("firefox") == "firefox"
    with pytest.raises(IsolationError, match="different engines"):
        runner._resolve_engine("webkit")
    monkeypatch.setenv("CLINIC_BROWSER_ENGINE", "netscape")
    with pytest.raises(IsolationError, match="not supported: netscape"):
        runner._resolve_engine(None)
    monkeypatch.delenv("CLINIC_BROWSER_ENGINE")
    with pytest.raises(IsolationError, match="not supported: Firefox"):
        runner._resolve_engine("Firefox")


# Engine-specific Playwright/browser APIs live only in engines.py; a suite
# that uses one directly would crash or silently fall back to Chromium.
ENGINE_SPECIFIC_APIS = (
    "driver.chromium",
    ".chromium.launch",
    "new_cdp_session",
    "chrome://",
    "--use-fake-device",
    "grant_permissions(",
    "clipboard.readText",
    "CLINIC_RENEWAL_BROWSER_EXECUTABLE",
    "wait_for_function(",
)


def test_browser_suites_leave_engine_specific_apis_to_the_engines_module() -> None:
    offenders = [
        (relpath, api)
        for paths in runner.SUITES.values()
        for relpath in paths
        for api in ENGINE_SPECIFIC_APIS
        if api in (REPOSITORY / relpath).read_text(encoding="utf-8")
    ]
    assert offenders == []


BROWSER_SUITES = REPOSITORY / "tests" / "renewal" / "browser"
ENGINES_MODULE = BROWSER_SUITES / "engines.py"
# The only functions that may capture beyond the viewport: both keep each
# capture within the engines' pixel limit (engines.py, Screenshot size).
FULL_PAGE_CAPTURERS = ("full_page_screenshot", "full_page_clip")
# Reviewed exemptions, per file: function -> reason.
CAPTURE_EXEMPTIONS: dict[Path, dict[str, str]] = {
    ENGINES_MODULE: dict.fromkeys(
        FULL_PAGE_CAPTURERS, "the splitter: sections within the pixel limit"
    ),
    BROWSER_SUITES / "conftest.py": {
        "csp_console_violations": (
            "patches Page/Locator.screenshot with a pass-through that attributes"
            " WebKit's screenshot style; every capture is still a call site here"
        ),
    },
}


def _full_page_captures(path: Path, exempt: tuple[str, ...]) -> list[str]:
    """Screenshots in ``path`` that may capture past the viewport.

    A ``screenshot`` call counts unless its ``full_page`` is absent or the
    literal ``False``. Forms that cannot be read statically count too: a
    ``**`` splat, ``screenshot`` referenced without a call, or looked up by
    name. Sites inside the ``exempt`` functions of ``path`` do not count.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    parents = {
        child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)
    }
    exempt_lines = {
        line
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name in exempt
        for line in range(node.lineno, (node.end_lineno or node.lineno) + 1)
    }
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and node.value == "screenshot":
            unbounded = True
        elif isinstance(node, ast.Attribute) and node.attr == "screenshot":
            call = parents[node]
            if not isinstance(call, ast.Call) or call.func is not node:
                unbounded = True
            else:
                unbounded = any(
                    keyword.arg is None
                    or (
                        keyword.arg == "full_page"
                        and not (
                            isinstance(keyword.value, ast.Constant)
                            and keyword.value.value is False
                        )
                    )
                    for keyword in call.keywords
                )
        else:
            continue
        if unbounded and node.lineno not in exempt_lines:
            found.append(f"{path.relative_to(REPOSITORY)}:{node.lineno}")
    return sorted(found)


def test_browser_suites_capture_full_pages_only_through_the_engines_splitter() -> None:
    # Firefox and WebKit refuse, and Chromium fails, a capture past 32767
    # device pixels; hosted primitives@chromium failed exactly so at 375px.
    scanned = sorted(BROWSER_SUITES.rglob("*.py"))
    registered = {
        REPOSITORY / relpath for paths in runner.SUITES.values() for relpath in paths
    }
    assert registered <= set(scanned)
    assert all(callable(getattr(engines, name)) for name in FULL_PAGE_CAPTURERS)
    # Every exemption names a function that exists and that the detector
    # flags once its exemption is lifted.
    for path, functions in CAPTURE_EXEMPTIONS.items():
        for function in functions:
            others = tuple(name for name in functions if name != function)
            assert len(_full_page_captures(path, exempt=others)) > len(
                _full_page_captures(path, exempt=tuple(functions))
            ), (path, function)

    offenders = [
        site
        for path in scanned
        for site in _full_page_captures(
            path, exempt=tuple(CAPTURE_EXEMPTIONS.get(path, {}))
        )
    ]

    assert offenders == []


NAVIGATION_MODULE = BROWSER_SUITES / "_navigation.py"
NAVIGATION_IMPORT = "renewal.browser._navigation"
NAVIGATION_HELPER = "expect_document"
# Events whose wait cannot stand in for a document navigation.
SAFE_EVENTS = frozenset({"requestfailed", "download", "console", "pageerror", "dialog"})
ENTER_KEYS = frozenset({"Enter", "Return", "NumpadEnter"})
URL_WAITS = frozenset({"wait_for_url", "wait_for_load_state", "to_have_url"})
DYNAMIC_ATTRIBUTE = frozenset({"getattr", "setattr", "delattr", "hasattr"})
DYNAMIC_ACCESSORS = frozenset(
    {"attrgetter", "methodcaller", "__getattribute__", "__getattr__"}
)
# In-page ways to start a navigation, as the suites write JavaScript.
NAVIGATING_JS = re.compile(
    r"requestSubmit|\.submit\s*\(|\.click\s*\(\s*\)"
    r"|dispatchEvent\s*\(\s*new\s+\w*Event\s*\(\s*['\"](?:submit|click)"
    r"|location\s*\.\s*(?:assign|replace|reload)\b|location(?:\s*\.\s*href)?\s*=[^=]"
    r"|history\s*\.\s*(?:back|forward|go)\s*\(|window\s*\.\s*open\s*\("
)
WHOLE_FILE = "*"
# Reviewed exemptions, per file: scope (function, or module-level constant)
# -> reason. Each must still be load-bearing (see the guard test).
NAVIGATION_EXEMPTIONS: dict[Path, dict[str, str]] = {
    NAVIGATION_MODULE: {
        "expect_document": "the helper: expect_navigation between two settles",
        "goto_settled": "the helper: goto between two settles",
        "reload_settled": "the helper: reload between two settles",
        "go_back_settled": "the helper: go_back between two settles",
        "go_forward_settled": "the helper: go_forward between two settles",
        "wait_for_signed_in": "the helper: sign-in landing, then a settle",
    },
    BROWSER_SUITES / "engines.py": {
        "browser_zoom_200": (
            "chrome://settings is browser-internal: no app worker to settle,"
            " and a settle there cannot read a registration"
        ),
    },
    BROWSER_SUITES / "test_smoke.py": {
        "_unsafe_press": "reproduces the pre-fix press against a held activation",
        "_unsafe_goto": "reproduces an unsettled goto against a held activation",
        "test_navigation_helper_waits_out_a_held_worker_activation": (
            "waits for the held POST that _unsafe_press left pending"
        ),
        "test_owner_login_establishes_a_session_and_enters_the_totp_flow": (
            "asserts the session cookie between the press and the URL wait"
        ),
    },
    BROWSER_SUITES / "test_billing.py": {
        "test_preferences_keyboard_and_reflow_hold_on_the_payment_screen": (
            "Enter on the copy button copies the code; no document"
        ),
    },
    BROWSER_SUITES / "test_clinical_history.py": {
        "zoom_journey": "Enter on a <summary> toggles <details>; no document",
    },
    BROWSER_SUITES / "test_primitives.py": dict.fromkeys(
        (
            "test_calendar_days_and_combobox_hold_at_320",
            "test_combobox_filters_moves_and_chooses",
            "test_date_picker_moves_by_day_and_month_and_respects_bounds",
            "test_dialog_opens_modally_and_returns_focus",
            "test_drawer_is_non_modal_and_escape_returns_focus",
        ),
        "Enter on showcase dialogs, drawers, pickers and comboboxes; no document",
    ),
    BROWSER_SUITES / "test_retention.py": {
        "_matrix_widths": "Enter on the export button starts a download",
    },
    BROWSER_SUITES / "test_staff_intake.py": {
        "_find_across_pages": "Enter on the htmx pagination button; no document",
    },
    BROWSER_SUITES / "test_patient_access.py": {
        "_patient_journey": (
            "asserts the redeem POST's 302 between the press and the URL wait"
        ),
    },
    BROWSER_SUITES / "test_workspace.py": {
        WHOLE_FILE: (
            "todo 13 owns the workspace suites and adopts the helpers at rebase"
        ),
    },
}


@functools.cache
def _waiting_methods() -> frozenset[str]:
    """Playwright actions that take ``no_wait_after`` (derived, pinned API)."""
    return frozenset(
        name
        for owner in (Locator, Page, Frame, ElementHandle)
        for name, member in vars(owner).items()
        if not name.startswith("_")
        and callable(member)
        and "no_wait_after" in inspect.signature(member).parameters
    )


@functools.cache
def _navigating_methods() -> frozenset[str]:
    """Page/Frame methods that start or await a document navigation.

    Derived from the pinned sync API: a navigation returns its main resource
    (``Optional[Response]``); a navigation subscription is the ``expect_*``
    that yields a Response for a navigation; the generic event waits take an
    ``event`` (``on``/``once`` only register listeners).
    """
    found: set[str] = set()
    for owner in (Page, Frame):
        for name, member in vars(owner).items():
            if name.startswith("_") or not callable(member):
                continue
            signature = inspect.signature(member)
            returned = str(signature.return_annotation)
            main_resource = returned.startswith("typing.Optional")
            if "'Response'" in returned and (main_resource or "navigation" in name):
                found.add(name)
            if "event" in signature.parameters and name not in {"on", "once"}:
                found.add(name)
    return frozenset(found)


@functools.cache
def _settling_helpers() -> frozenset[str]:
    """Public functions of ``_navigation`` that settle the worker."""
    tree = ast.parse(NAVIGATION_MODULE.read_text(encoding="utf-8"))
    functions = {
        node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)
    }
    settling = {"settle_service_worker"}
    changed = True
    while changed:
        changed = False
        for name, node in functions.items():
            calls = {
                call.func.id
                for call in ast.walk(node)
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
            }
            if name not in settling and calls & settling:
                settling.add(name)
                changed = True
    return frozenset(name for name in settling if not name.startswith("_"))


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Name):
            return func.id
        if isinstance(func, ast.Attribute):
            return func.attr
    return None


def _settles(statement: ast.stmt, settling: frozenset[str]) -> bool:
    """A statement that ends in a settled document (or subscribes to one)."""
    if isinstance(statement, ast.With):
        return any(
            _call_name(item.context_expr) == NAVIGATION_HELPER
            for item in statement.items
        )
    if not isinstance(statement, ast.Expr | ast.Assign):
        return False
    return _call_name(statement.value) in settling - {None}


def _derived_settling(sources: dict[Path, str]) -> dict[Path, frozenset[str]]:
    """Per suite file: the helpers plus every function that reaches one.

    A suite function settles if its body calls a settling name; a name is
    resolved in its own file, or in the suite module it is imported from.
    Fixed point over all files.
    """
    helpers = _settling_helpers()
    trees = {path: ast.parse(source) for path, source in sources.items()}
    module_of = {f"renewal.browser.{path.stem}": path for path in trees}
    imports: dict[Path, dict[str, tuple[Path, str]]] = {}
    for path, tree in trees.items():
        imports[path] = {
            alias.asname or alias.name: (module_of[node.module], alias.name)
            for node in tree.body
            if isinstance(node, ast.ImportFrom) and node.module in module_of
            for alias in node.names
        }
    local: dict[Path, set[str]] = {path: set() for path in trees}
    changed = True
    while changed:
        changed = False
        for path, tree in trees.items():
            known = (
                helpers
                | local[path]
                | {
                    name
                    for name, (origin, original) in imports[path].items()
                    if original in local[origin]
                }
            )
            for node in tree.body:
                if not isinstance(node, ast.FunctionDef) or node.name in local[path]:
                    continue
                if any(
                    _call_name(call) in known
                    for call in ast.walk(node)
                    if isinstance(call, ast.Call)
                ):
                    local[path].add(node.name)
                    changed = True
    return {
        path: helpers
        | frozenset(local[path])
        | frozenset(
            name
            for name, (origin, original) in imports[path].items()
            if original in local[origin]
        )
        for path in trees
    }


def _is_url_wait(statement: ast.stmt) -> bool:
    return any(
        _call_name(node) in URL_WAITS
        for node in ast.walk(statement)
        if isinstance(node, ast.Call)
    ) and isinstance(statement, ast.Expr)


def _scopes(tree: ast.Module) -> dict[int, str]:
    """Line -> exemption scope: innermost function, else module-level target."""
    scope: dict[int, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign | ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [t.id for t in targets if isinstance(t, ast.Name)]
            if names:
                for line in range(node.lineno, (node.end_lineno or node.lineno) + 1):
                    scope[line] = names[0]
    functions = sorted(
        (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)),
        key=lambda n: (n.end_lineno or n.lineno) - n.lineno,
        reverse=True,
    )
    for function in functions:  # outermost first, innermost wins
        for line in range(
            function.lineno, (function.end_lineno or function.lineno) + 1
        ):
            scope[line] = function.name
    return scope


def _helper_zone(tree: ast.Module) -> set[int]:
    """Lines inside a ``with expect_document(...)`` body."""
    return {
        line
        for node in ast.walk(tree)
        if isinstance(node, ast.With)
        and any(
            _call_name(item.context_expr) == NAVIGATION_HELPER for item in node.items
        )
        for statement in node.body
        for line in range(statement.lineno, (statement.end_lineno or 0) + 1)
    }


def _key_rule(node: ast.Call, name: str | None) -> str | None:
    """Enter pressed, or a newline typed, outside ``expect_document``."""
    if name in {"press", "down"}:
        key = node.args[0] if node.args else None
        if not isinstance(key, ast.Constant) or (
            str(key.value).split("+")[-1] in ENTER_KEYS
        ):
            return "Enter key outside expect_document"
    if name in {"type", "press_sequentially", "insert_text"} and node.args:
        text = node.args[0]
        if isinstance(text, ast.Constant) and any(c in str(text.value) for c in "\n\r"):
            return "newline typed outside expect_document"
    if name == "dispatch_event":
        return "dispatch_event outside expect_document"
    return None


def _call_rules(node: ast.Call, *, in_zone: bool, waiting: frozenset[str]) -> list[str]:
    name = _call_name(node)
    keys = [keyword.arg for keyword in node.keywords]
    rules: list[str] = []
    dynamic = len(node.args) < 2 or not isinstance(node.args[1], ast.Constant)
    if name in DYNAMIC_ATTRIBUTE and isinstance(node.func, ast.Name) and dynamic:
        rules.append(f"dynamic {name}")
    if "no_wait_after" in keys:
        rules.append("no_wait_after")
    if None in keys and (name in waiting or name == "click_when_hittable"):
        rules.append(f"splat into {name}")
    func = node.func
    keyed = isinstance(func, ast.Attribute) and not (
        isinstance(func.value, ast.Attribute) and func.value.attr == "mouse"
    )
    key = None if in_zone or not keyed else _key_rule(node, name)
    if key:
        rules.append(key)
    return rules


def _import_rules(node: ast.Import | ast.ImportFrom) -> list[str]:
    if isinstance(node, ast.Import):
        return [
            "module-qualified _navigation"
            for alias in node.names
            if alias.name == NAVIGATION_IMPORT
        ]
    module = node.module or ""
    rules: list[str] = []
    for alias in node.names:
        if module == "functools" and alias.name in {"partial", "partialmethod"}:
            rules.append(f"import {alias.name}")
        if module == NAVIGATION_IMPORT and alias.asname not in {None, alias.name}:
            rules.append(f"aliased {alias.name}")
        if module == "renewal.browser" and alias.name == "_navigation":
            rules.append("module-qualified _navigation")
    return rules


def _event_is_safe(tree: ast.Module, node: ast.Attribute) -> bool:
    """An ``expect_event``/``wait_for_event`` on a literal non-navigation event."""
    if node.attr not in {"expect_event", "wait_for_event"}:
        return False
    call = next(
        (c for c in ast.walk(tree) if isinstance(c, ast.Call) and c.func is node),
        None,
    )
    if call is None:
        return False
    event = call.args[0] if call.args else None
    event = event or next((k.value for k in call.keywords if k.arg == "event"), None)
    return isinstance(event, ast.Constant) and event.value in SAFE_EVENTS


def _url_wait_rules(node: ast.AST, settling: frozenset[str]) -> list[int]:
    """URL waits that do not directly follow a settled navigation."""
    lines: list[int] = []
    for field in ("body", "orelse", "finalbody"):
        block = getattr(node, field, None)
        if not isinstance(block, list):
            continue
        for index, statement in enumerate(block):
            if not _is_url_wait(statement):
                continue
            previous = block[index - 1] if index > 0 else None
            before = block[index - 2] if index > 1 else None
            settled = previous is not None and (
                _settles(previous, settling)
                or (
                    _is_url_wait(previous)
                    and before is not None
                    and _settles(before, settling)
                )
            )
            if not settled:
                lines.append(statement.lineno)
    return lines


def _reference_rules(
    tree: ast.Module,
    node: ast.Attribute | ast.Name | ast.Constant,
    navigating: frozenset[str],
    *,
    in_zone: bool,
    docstrings: set[int],
) -> list[str]:
    """Rules on a name, attribute or string: how a navigation is reached."""
    if isinstance(node, ast.Constant):
        text = node.value if isinstance(node.value, str) else ""
        rules: list[str] = []
        if id(node) not in docstrings and text in navigating:
            rules.append(f"navigation method named {text!r}")
        elif id(node) not in docstrings and NAVIGATING_JS.search(text) and not in_zone:
            rules.append("navigating JavaScript")
        return rules
    name = node.attr if isinstance(node, ast.Attribute) else node.id
    if isinstance(node, ast.Attribute) and name in navigating:
        return [] if _event_is_safe(tree, node) else [f"navigation method {name}"]
    partial = isinstance(node, ast.Attribute) and name in {"partial", "partialmethod"}
    if name in DYNAMIC_ACCESSORS or partial:
        return [f"indirect access via {name}"]
    return []


def _unsafe_navigation_waits(
    source: str,
    label: str,
    exempt: tuple[str, ...],
    settling: frozenset[str] | None = None,
) -> list[str]:
    """Navigations in ``source`` that can bypass ``_navigation``'s worker settle.

    Fails closed: a navigating Page/Frame method used directly or named in a
    string (``getattr``, ``attrgetter``, ``methodcaller``); ``getattr`` and
    friends with a non-literal name; ``partial``; an event wait on anything
    but a literal non-navigation event; Enter pressed, a newline typed or
    ``dispatch_event``; in-page JavaScript that submits, clicks or moves
    ``location``/``history``; ``no_wait_after`` or a splat into an action; an
    aliased or module-qualified ``_navigation`` import; and a URL wait that
    does not directly follow a settled navigation (the shape of a bare
    navigating click). Keys, ``dispatch_event`` and JavaScript are allowed
    inside ``with expect_document``. Docstrings are not code. Sites in
    ``exempt`` scopes do not count.
    """
    tree = ast.parse(source, filename=label)
    scopes = _scopes(tree)
    zone = _helper_zone(tree)
    docstrings = {
        id(owner.body[0].value)
        for owner in ast.walk(tree)
        if isinstance(owner, ast.Module | ast.FunctionDef | ast.ClassDef)
        and owner.body
        and isinstance(owner.body[0], ast.Expr)
        and isinstance(owner.body[0].value, ast.Constant)
    }
    navigating = _navigating_methods()
    waiting = _waiting_methods()
    settling = settling if settling is not None else _settling_helpers()
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Attribute | ast.Name | ast.Constant):
            found.extend(
                (line, rule)
                for rule in _reference_rules(
                    tree, node, navigating, in_zone=line in zone, docstrings=docstrings
                )
            )
        elif isinstance(node, ast.Call):
            found.extend(
                (line, rule)
                for rule in _call_rules(node, in_zone=line in zone, waiting=waiting)
            )
        elif isinstance(node, ast.Import | ast.ImportFrom):
            found.extend((line, rule) for rule in _import_rules(node))
        found.extend(
            (wait, "URL wait after an unsettled step")
            for wait in _url_wait_rules(node, settling)
        )
    if WHOLE_FILE in exempt:
        return []
    return sorted(
        f"{label}:{line}: {rule}"
        for line, rule in set(found)
        if scopes.get(line) not in exempt
    )


@functools.cache
def _suite_sources() -> dict[Path, str]:
    return {
        path: path.read_text(encoding="utf-8")
        for path in sorted(BROWSER_SUITES.rglob("*.py"))
    }


@functools.cache
def _suite_settling() -> dict[Path, frozenset[str]]:
    return _derived_settling(_suite_sources())


def _navigation_offenders(
    path: Path, source: str, exempt: tuple[str, ...], settling: frozenset[str]
) -> list[str]:
    return _unsafe_navigation_waits(
        source, str(path.relative_to(REPOSITORY)), exempt, settling
    )


# Each shape the gate review used to slip past the spelling-only guard (plus
# the earlier ones), planted as a new function in a real suite file.
EVASIONS = {
    "concatenated getattr": (
        "    with getattr(page, 'expect_' + 'navigation')():\n"
        "        page.locator('a').click()\n"
    ),
    "literal getattr": "    getattr(page, 'goto')('/')\n",
    "attrgetter": "    operator.attrgetter('go' + 'to')(page)('/')\n",
    "expect_event framenavigated": (
        "    with page.expect_event('framenavigated'):\n"
        "        page.locator('a').click()\n"
    ),
    "wait_for_event load": (
        "    page.locator('a').click()\n    page.wait_for_event('load')\n"
    ),
    "bare navigating click + wait_for_url": (
        "    page.locator('a').click()\n    page.wait_for_url('**/x')\n"
    ),
    "bare click + to_have_url": (
        "    page.locator('a').click()\n    expect(page).to_have_url('/x')\n"
    ),
    "press Enter + wait_for_url": (
        "    page.keyboard.press('Enter')\n    page.wait_for_url('**/x')\n"
    ),
    "locator press Return": "    page.locator('#q').press('Return')\n",
    "non-literal key": "    page.keyboard.press(key)\n",
    "typed newline": "    page.locator('#q').type('text\\n')\n",
    "requestSubmit via evaluate": (
        "    page.evaluate(\"document.querySelector('form').requestSubmit()\")\n"
    ),
    "form.submit via evaluate": "    page.evaluate('f => f.submit()')\n",
    "location via evaluate": "    page.evaluate('location.href = \"/x\"')\n",
    "dispatch_event submit": "    page.locator('form').dispatch_event('submit')\n",
    "aliased helper + no_wait_after": (
        "    from renewal.browser._navigation import expect_document as ed\n"
        "    with ed(page):\n"
        "        page.locator('a').click(no_wait_after=True)\n"
    ),
    "module-qualified helper + no_wait_after": (
        "    from renewal.browser import _navigation\n"
        "    with _navigation.expect_document(page):\n"
        "        page.locator('a').click(no_wait_after=True)\n"
    ),
    "partial no_wait_after": (
        "    import functools\n"
        "    functools.partial(page.locator('a').click, no_wait_after=True)()\n"
    ),
    "imported partial": (
        "    from functools import partial\n"
        "    partial(page.locator('a').click, no_wait_after=True)()\n"
    ),
    "splat into click": "    page.locator('a').click(**options)\n",
    "raw expect_navigation": (
        "    with page.expect_navigation():\n        page.locator('a').click()\n"
    ),
    "raw goto": "    page.goto('/x')\n",
    "bound goto": "    go = page.goto\n    go('/x')\n",
    "raw reload": "    page.reload()\n",
    "raw go_back": "    page.go_back()\n",
    "lambda goto": "    run = lambda: page.goto('/x')\n    run()\n",
}
SAFE_SHAPES = {
    "click_to_navigate + wait_for_url": (
        "    click_to_navigate(page.locator('a'))\n    page.wait_for_url('**/x')\n"
    ),
    "Enter inside expect_document": (
        "    with expect_document(page):\n        page.keyboard.press('Enter')\n"
    ),
    "history.back inside expect_document": (
        "    with expect_document(page):\n        page.evaluate('history.back()')\n"
    ),
    "goto_settled": "    goto_settled(page, '/x')\n",
    "Tab key": "    page.keyboard.press('Tab')\n",
    "requestfailed event": (
        "    with page.expect_event('requestfailed'):\n        page.evaluate('1')\n"
    ),
    "sign-in landing": "    wait_for_signed_in(page)\n",
}


def _planted(host: str, body: str) -> str:
    return f"{host}\n\ndef _planted_fa12(page, key, options, expect):\n{body}"


@pytest.mark.parametrize(
    ("shape", "body", "unsafe"),
    [(name, body, True) for name, body in EVASIONS.items()]
    + [(name, body, False) for name, body in SAFE_SHAPES.items()],
)
def test_navigation_guard_catches_each_evasion_planted_in_a_suite(
    shape: str, body: str, *, unsafe: bool
) -> None:
    host_path = BROWSER_SUITES / "test_encounter.py"
    sources = _suite_sources()
    settling = _suite_settling()[host_path]
    exempt = tuple(NAVIGATION_EXEMPTIONS.get(host_path, {}))
    before = _navigation_offenders(host_path, sources[host_path], exempt, settling)
    planted = _planted(sources[host_path], body)
    after = _navigation_offenders(host_path, planted, exempt, settling)
    assert (len(after) > len(before)) is unsafe, (shape, after[len(before) :])


def test_browser_suites_navigate_only_through_the_navigation_helpers() -> None:
    # Hosted retention@firefox 36349167980: a press whose POST never left the
    # browser, the signature of a navigation held behind an activating worker.
    sources = _suite_sources()
    registered = {
        REPOSITORY / relpath for paths in runner.SUITES.values() for relpath in paths
    }
    assert registered <= set(sources)
    assert {"goto", "reload", "go_back", "go_forward", "expect_navigation"} <= (
        _navigating_methods()
    )
    assert {"expect_event", "wait_for_event"} <= _navigating_methods()
    assert {"click", "press", "check"} <= _waiting_methods()
    assert not {
        name
        for name, member in vars(Keyboard).items()
        if callable(member) and "no_wait_after" in inspect.signature(member).parameters
    }
    assert {
        "click_to_navigate",
        "expect_document",
        "goto_settled",
        "reload_settled",
        "go_back_settled",
        "go_forward_settled",
        "wait_for_signed_in",
    } <= _settling_helpers()
    settling = _suite_settling()
    # Every exemption names a scope the detector flags once it is lifted.
    for path, scopes in NAVIGATION_EXEMPTIONS.items():
        for scope in scopes:
            others = tuple(name for name in scopes if name != scope)
            assert len(
                _navigation_offenders(path, sources[path], others, settling[path])
            ) > len(
                _navigation_offenders(
                    path, sources[path], tuple(scopes), settling[path]
                )
            ), (path, scope)

    offenders = [
        site
        for path, source in sources.items()
        for site in _navigation_offenders(
            path, source, tuple(NAVIGATION_EXEMPTIONS.get(path, {})), settling[path]
        )
    ]

    assert offenders == []


@pytest.mark.parametrize("engine", ["firefox", "webkit"])
@pytest.mark.parametrize("suite", sorted(runner.SUITES))
def test_every_suite_runs_on_every_engine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    suite: str,
    engine: str,
) -> None:
    monkeypatch.delenv("CLINIC_BROWSER_ENGINE", raising=False)
    started: list[tuple[str, str]] = []

    def fake_suite(*args: object) -> JsonObject:
        started.append((str(args[1]), str(args[5])))
        return {"suite": str(args[1]), "engine": str(args[5])}

    monkeypatch.setattr(runner, "_run_browser_suite", fake_suite)
    artifacts = _private(tmp_path / "artifacts")
    arguments = ["browser", "--suite", suite, "--engine", engine]
    assert runner.main([*arguments, "--artifact-root", str(artifacts)]) == 0
    assert started == [(suite, engine)]


@pytest.mark.parametrize(
    ("arguments", "environment"),
    [
        (["--engine", "netscape"], None),
        ([], "netscape"),
        (["--engine", "firefox"], "webkit"),
        (["--engine", ""], None),
    ],
)
def test_main_rejects_bad_engines_before_any_provisioning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arguments: list[str],
    environment: str | None,
) -> None:
    if environment is None:
        monkeypatch.delenv("CLINIC_BROWSER_ENGINE", raising=False)
    else:
        monkeypatch.setenv("CLINIC_BROWSER_ENGINE", environment)

    def _explode(*args: object, **kwargs: object) -> None:
        message = "a rejected engine must never provision or capture"
        raise AssertionError(message)

    monkeypatch.setattr(runner, "_docker", _explode)
    monkeypatch.setattr(runner, "_capture_source", _explode)
    assert (
        runner.main(
            [
                "browser",
                "--suite",
                "smoke",
                *arguments,
                "--artifact-root",
                str(tmp_path / "artifacts"),
            ]
        )
        == 2
    )


def test_unavailable_engine_fails_the_suite_before_provisioning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CLINIC_BROWSER_ENGINE", raising=False)
    monkeypatch.delenv("CLINIC_RENEWAL_BROWSER_EXECUTABLE", raising=False)
    monkeypatch.setattr(
        runner, "_managed_executable", lambda _engine: str(tmp_path / "absent")
    )

    def _explode(*args: object, **kwargs: object) -> None:
        message = "an unavailable engine must never provision"
        raise AssertionError(message)

    monkeypatch.setattr(runner, "_docker", _explode)
    monkeypatch.setattr(runner, "_capture_source", _explode)
    artifacts = tmp_path / "artifacts"
    assert (
        runner.main(
            [
                "browser",
                "--suite",
                "smoke",
                "--engine",
                "webkit",
                "--artifact-root",
                str(artifacts),
            ]
        )
        == 2
    )
    assert not (artifacts / "report.json").exists()


def test_managed_engine_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "firefox"
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o755)
    requested: list[str] = []

    def managed(engine: str) -> str:
        requested.append(engine)
        return str(executable)

    monkeypatch.delenv("CLINIC_RENEWAL_BROWSER_EXECUTABLE", raising=False)
    monkeypatch.setattr(runner, "_managed_executable", managed)
    assert runner._resolve_browser("firefox") == str(executable)
    assert requested == ["firefox"]
    executable.chmod(0o644)
    with pytest.raises(IsolationError, match="playwright install firefox"):
        runner._resolve_browser("firefox")
    executable.chmod(0o755)
    # The Chromium override never stands in for another engine.
    monkeypatch.setenv("CLINIC_RENEWAL_BROWSER_EXECUTABLE", str(executable))
    with pytest.raises(IsolationError, match="Chromium-only"):
        runner._resolve_browser("webkit")
    assert requested == ["firefox", "firefox"]


@pytest.mark.parametrize("engine", ["firefox", "webkit"])
def test_managed_executable_names_the_playwright_engine_build(engine: str) -> None:
    path = Path(runner._managed_executable(engine))
    assert path.is_absolute()
    assert f"{engine}-" in str(path)


def test_ci_browser_gate_always_runs_chromium(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLINIC_BROWSER_ENGINE", "firefox")
    engines: dict[str, object] = {}

    def fake_suite(*args: object) -> JsonObject:
        engines[str(args[1])] = args[5]
        return {"suite": str(args[1])}

    monkeypatch.setattr(runner, "_run_browser_suite", fake_suite)
    results = runner._gate_browser(REPOSITORY, tmp_path, tmp_path / "run")
    assert {result["exit"] for result in results} == {0}
    assert engines == dict.fromkeys(runner.SUITES, "chromium")


def test_axe_support_blocks_only_serious_and_critical_impacts() -> None:
    violations: list[dict[str, object]] = [
        {"id": "image-alt", "impact": "critical"},
        {"id": "color-contrast", "impact": "serious"},
        {"id": "region", "impact": "moderate"},
        {"id": "heading-order", "impact": "minor"},
    ]
    assert [v["id"] for v in a11y_support.blocking(violations)] == [
        "image-alt",
        "color-contrast",
    ]
    assert a11y_support.AXE_URL == "/static/vendor/axe/axe.min.js"
    assert (
        REPOSITORY / "static" / a11y_support.AXE_URL.removeprefix("/static/")
    ).is_file()


def test_suite_side_engine_selection_fails_instead_of_skipping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CLINIC_BROWSER_ENGINE", raising=False)
    assert engines.selected_engine() == "chromium"
    monkeypatch.setenv("CLINIC_BROWSER_ENGINE", "webkit")
    assert engines.selected_engine() == "webkit"
    monkeypatch.setenv("CLINIC_BROWSER_ENGINE", "netscape")
    with pytest.raises(pytest.fail.Exception, match="netscape"):
        engines.selected_engine()


def test_mobile_profiles_are_the_touch_phones_of_the_device_matrix() -> None:
    assert {p.label for p in engines.MOBILE_PROFILES.values()} == {
        "iPhone 15",
        "Pixel 8",
    }
    assert engines.ENGINES == runner.ENGINES
    assert engines.DEFAULT_ENGINE == runner.DEFAULT_ENGINE
    # The static profiles mirror the pinned Playwright device descriptors.
    with sync_playwright() as driver:
        for profile in engines.MOBILE_PROFILES.values():
            descriptor = driver.devices[profile.label]
            assert descriptor["viewport"] == {
                "width": profile.width,
                "height": profile.height,
            }
            assert descriptor["device_scale_factor"] == profile.device_scale_factor
            assert descriptor["user_agent"] == profile.user_agent
            assert descriptor["is_mobile"] is True
            assert descriptor["has_touch"] is True


def test_artifact_root_must_be_excluded_or_external(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner, "ensure_private_directory", lambda _path: True)
    inside = REPOSITORY / "renewal-artifacts"
    with pytest.raises(IsolationError, match="artifact root"):
        runner._artifact_root(str(inside), REPOSITORY)
    excluded = REPOSITORY / ".omo" / "renewal-runs" / "x"
    assert runner._artifact_root(str(excluded), REPOSITORY) == excluded
    outside = tmp_path / "artifacts"
    assert runner._artifact_root(str(outside), REPOSITORY) == outside
    with pytest.raises(IsolationError):
        runner._artifact_root("relative/path", REPOSITORY)


def test_artifact_root_rejects_symlink(tmp_path: Path) -> None:
    target = _private(tmp_path / "real")
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(IsolationError):
        runner._artifact_root(str(link), REPOSITORY)


def test_junit_verdict_rejects_zero_tests_failures_errors_and_skips(
    tmp_path: Path,
) -> None:
    def _write(name: str, body: str) -> Path:
        path = tmp_path / name
        path.write_text(body)
        return path

    ok = _write(
        "ok.xml",
        '<testsuite tests="3" failures="0" errors="0" skipped="0"></testsuite>',
    )
    assert runner._junit_verdict(ok) == (3, 0, 0, 0)

    zero = _write(
        "zero.xml",
        '<testsuite tests="0" failures="0" errors="0" skipped="0"></testsuite>',
    )
    assert runner._junit_verdict(zero)[0] == 0

    failed = _write(
        "failed.xml",
        '<testsuite tests="2" failures="1" errors="0" skipped="0"></testsuite>',
    )
    assert runner._junit_verdict(failed)[1] == 1

    errored = _write(
        "errored.xml",
        '<testsuite tests="2" failures="0" errors="1" skipped="0"></testsuite>',
    )
    assert runner._junit_verdict(errored)[2] == 1

    skipped = _write(
        "skipped.xml",
        '<testsuite tests="2" failures="0" errors="0" skipped="2"></testsuite>',
    )
    assert runner._junit_verdict(skipped)[3] == 2

    with pytest.raises(IsolationError):
        runner._junit_verdict(tmp_path / "absent.xml")
    garbage = _write("garbage.xml", "not xml at all")
    with pytest.raises(IsolationError):
        runner._junit_verdict(garbage)


def test_pytest_environment_exports_only_the_private_fixture_inputs(
    tmp_path: Path,
) -> None:
    environment = runner._pytest_environment(
        base_url="http://127.0.0.1:48000",
        artifact_root=tmp_path,
        browser="/usr/bin/google-chrome",
        engine="webkit",
        username="renewal-owner-x",
        password="secret-password",  # noqa: S106 - synthetic fixture value.
    )
    assert environment["CLINIC_BROWSER_ENGINE"] == "webkit"
    assert environment["CLINIC_RENEWAL_BASE_URL"] == "http://127.0.0.1:48000"
    assert environment["CLINIC_RENEWAL_ARTIFACT_ROOT"] == str(tmp_path)
    assert environment["CLINIC_RENEWAL_BROWSER_EXECUTABLE"] == "/usr/bin/google-chrome"
    assert environment["CLINIC_RENEWAL_USERNAME"] == "renewal-owner-x"
    assert environment["CLINIC_RENEWAL_PASSWORD"] == "secret-password"  # noqa: S105
    # Browser-suite worker subprocesses run config.settings.base and inherit
    # this environment; without the pin they would publish to whatever listens
    # on the developer workstation's 6379.
    assert environment["CELERY_BROKER_URL"] == "memory://"
    for leaked in (
        "APP_DATABASE_URL",
        "MIGRATION_DATABASE_URL",
        "TEST_SUPERUSER_DATABASE_URL",
        "CLINIC_OWNER_PASSWORD",
        "CLINIC_APP_PASSWORD",
        "POSTGRES_PASSWORD",
    ):
        assert leaked not in environment


def test_gate_coverage_binds_the_pytest_child_to_the_memory_broker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The coverage child must never publish into the workstation's Redis.

    ``_child_env`` rebuilds the environment from scratch, so a host
    ``CELERY_BROKER_URL`` is already stripped; the explicit pin mirrors
    ``_server_environment`` and survives ``config.settings`` defaults.
    """
    database = runner.ProvisionedDatabase(
        app_dsn="postgresql://clinic_app:pw@127.0.0.1:55432/clinic",
        app_password="pw",  # noqa: S106 - synthetic fixture value.
        container="clinic_renewal_db_covpin",
        database="clinic",
        owner_dsn="postgresql://clinic_owner:pw@127.0.0.1:55432/clinic",
        owner_password="pw",  # noqa: S106 - synthetic fixture value.
        port=55432,
        postgres_password="pw",  # noqa: S106 - synthetic fixture value.
        super_dsn="postgresql://clinic_super:pw@127.0.0.1:55432/clinic",
        super_password="pw",  # noqa: S106 - synthetic fixture value.
        volume="clinic_renewal_db_covpin_data",
    )

    @contextlib.contextmanager
    def fake_provision(
        repository: Path, token: str, *, docker: object = None
    ) -> Iterator[runner.ProvisionedDatabase]:
        yield database

    captured: list[dict[str, str]] = []

    def fake_run(
        argv: list[str],
        environment: dict[str, str],
        log: Path,
        *,
        timeout: int,
        cwd: Path | None = None,
    ) -> int:
        captured.append(environment)
        return 0

    monkeypatch.setattr(runner, "_provision_database", fake_provision)
    monkeypatch.setattr(runner, "_create_test_database", lambda *args: None)
    monkeypatch.setattr(runner, "_migrate", lambda *args: None)
    monkeypatch.setattr(runner, "_run_bounded", fake_run)
    results = runner._gate_coverage(REPOSITORY, tmp_path, "covpin")
    assert results == [{"command": "coverage", "exit": 0}]
    assert len(captured) == 1
    environment = captured[0]
    assert environment["DJANGO_SETTINGS_MODULE"] == "config.settings.test"
    assert environment["CELERY_BROKER_URL"] == "memory://"
    assert environment["APP_DATABASE_URL"] == database.app_dsn
    assert environment["MIGRATION_DATABASE_URL"] == database.owner_dsn
    assert environment["TEST_SUPERUSER_DATABASE_URL"] == database.super_dsn
    assert "CELERY_RESULT_BACKEND" not in environment


def test_bounded_subprocess_kills_a_hung_child_group(tmp_path: Path) -> None:
    log = tmp_path / "hung.log"
    started = time.monotonic()
    code = runner._run_bounded(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        {},
        log,
        timeout=2,
    )
    assert code != 0
    assert time.monotonic() - started < 30
    assert log.read_text(errors="replace") == ""


def test_provisioned_database_uses_unique_names_and_cleans_only_its_own(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_docker(*arguments: str, timeout: int = 60) -> str:
        calls.append(arguments)
        if arguments[:2] == ("container", "inspect"):
            return "abc123\n"
        if arguments[:2] == ("exec",) or arguments[0] == "exec":
            return ""
        return ""

    with runner._provision_database(REPOSITORY, "deadbeef", docker=fake_docker) as db:
        assert db.container.startswith("clinic_renewal_db_deadbeef")
        assert db.volume.startswith("clinic_renewal_db_deadbeef")
        assert db.app_dsn.startswith("postgresql://clinic_app:")
        assert db.owner_dsn.startswith("postgresql://clinic_owner:")
        assert db.super_dsn.startswith("postgresql://clinic_super:")
        assert f"127.0.0.1:{db.port}" in db.app_dsn
    removed = [call for call in calls if call[:2] == ("container", "rm")]
    volumes = [call for call in calls if call[:2] == ("volume", "rm")]
    assert removed == [("container", "rm", "-f", db.container)]
    assert volumes == [("volume", "rm", db.volume)]
    assert all("app_mei" not in " ".join(call) for call in calls)


def test_provision_cleans_up_when_readiness_fails(tmp_path: Path) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_docker(*arguments: str, timeout: int = 60) -> str:
        calls.append(arguments)
        if arguments[0] == "exec":
            message = "database never became ready"
            raise IsolationError(message)
        return ""

    with (
        pytest.raises(IsolationError, match="ready"),
        runner._provision_database(REPOSITORY, "cafef00d", docker=fake_docker),
    ):
        pass
    assert ("container", "rm", "-f", "clinic_renewal_db_cafef00d") in calls
    assert ("volume", "rm", "clinic_renewal_db_cafef00d_data") in calls


def test_provision_teardown_reports_docker_failures() -> None:
    """Removal failures at the Docker boundary must surface, not vanish."""
    calls: list[tuple[str, ...]] = []

    def fake_docker(*arguments: str, timeout: int = 60) -> str:
        calls.append(arguments)
        if arguments[:2] in {("container", "rm"), ("volume", "rm")}:
            message = "repro: Docker removal failed"
            raise runner.RenewalRunnerError(message)
        return ""

    with (
        pytest.raises(IsolationError, match="teardown failed"),
        runner._provision_database(REPOSITORY, "badteard", docker=fake_docker),
    ):
        pass
    assert ("container", "rm", "-f", "clinic_renewal_db_badteard") in calls
    assert ("volume", "rm", "clinic_renewal_db_badteard_data") in calls


def test_provision_teardown_failure_surfaces_over_body_error() -> None:
    calls: list[tuple[str, ...]] = []

    def fake_docker(*arguments: str, timeout: int = 60) -> str:
        calls.append(arguments)
        if arguments[:2] == ("volume", "rm"):
            message = "repro: volume removal timed out"
            raise runner.RenewalRunnerError(message)
        return ""

    body_error = ValueError("body exploded")
    with (
        pytest.raises(IsolationError, match="teardown failed") as caught,
        runner._provision_database(REPOSITORY, "bodyfail", docker=fake_docker),
    ):
        raise body_error
    assert caught.value.__context__ is body_error
    assert ("container", "rm", "-f", "clinic_renewal_db_bodyfail") in calls
    assert ("volume", "rm", "clinic_renewal_db_bodyfail_data") in calls


def test_provision_teardown_completes_through_a_second_interrupt() -> None:
    """A SIGTERM inside teardown still attempts every remaining removal."""
    calls: list[tuple[str, ...]] = []

    def fake_docker(*arguments: str, timeout: int = 60) -> str:
        calls.append(arguments)
        if arguments[:2] == ("container", "rm"):
            os.kill(os.getpid(), signal.SIGTERM)
        return ""

    previous = signal.signal(signal.SIGTERM, runner._interrupted)
    try:
        with (
            pytest.raises(runner.RenewalInterruptError),
            runner._provision_database(REPOSITORY, "sigterm2", docker=fake_docker),
        ):
            pass
    finally:
        signal.signal(signal.SIGTERM, previous)
    assert ("container", "rm", "-f", "clinic_renewal_db_sigterm2") in calls
    assert ("volume", "rm", "clinic_renewal_db_sigterm2_data") in calls


def test_provision_teardown_tolerates_absent_resources() -> None:
    """A resource the daemon reports as absent is already removed."""

    def fake_docker(*arguments: str, timeout: int = 60) -> str:
        if arguments[:2] == ("container", "rm"):
            message = "renewal docker command failed: No such container"
            raise runner.RenewalRunnerError(message)
        if arguments[:2] == ("volume", "rm"):
            message = "renewal docker command failed: no such volume"
            raise runner.RenewalRunnerError(message)
        return ""

    with runner._provision_database(REPOSITORY, "gonegone", docker=fake_docker):
        pass


def test_await_database_propagates_interruption() -> None:
    def fake_docker(*arguments: str, timeout: int = 60) -> str:
        message = "renewal runner interrupted"
        raise runner.RenewalInterruptError(message)

    with pytest.raises(runner.RenewalInterruptError):
        runner._await_database("clinic_renewal_db_x", docker=fake_docker)


def test_terminate_master_retries_once_then_propagates_the_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful retry still propagates the observed cancellation."""
    master = object.__new__(SupervisedMaster)
    terminations: list[str] = []

    def flaky_terminate() -> None:
        terminations.append("terminate")
        if len(terminations) == 1:
            message = "renewal runner interrupted"
            raise runner.RenewalInterruptError(message)

    monkeypatch.setattr(master, "terminate", flaky_terminate)
    with pytest.raises(runner.RenewalInterruptError):
        runner._terminate_master(master)
    assert terminations == ["terminate", "terminate"]

    def stuck_terminate() -> None:
        message = "renewal runner interrupted"
        raise runner.RenewalInterruptError(message)

    monkeypatch.setattr(master, "terminate", stuck_terminate)
    with pytest.raises(runner.RenewalInterruptError):
        runner._terminate_master(master)


def test_teardown_interrupt_is_reaped_then_propagated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A first SIGTERM during otherwise-successful teardown still exits 130.

    The child is real: the interrupted first ``terminate`` is retried so the
    bounded TERM/reap completes, then the cancellation propagates instead of
    becoming a successful run.
    """
    log = tmp_path / "teardown.log"
    with log.open("wb") as stream:
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            start_new_session=True,
            stdout=stream,
            stderr=subprocess.STDOUT,
        )
    master = SupervisedMaster(process)
    original = SupervisedMaster.terminate
    calls = 0

    def terminate_with_signal(self: SupervisedMaster) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            os.kill(os.getpid(), signal.SIGTERM)
        original(self)

    previous = signal.signal(signal.SIGTERM, runner._interrupted)
    monkeypatch.setattr(SupervisedMaster, "terminate", terminate_with_signal)
    try:
        with pytest.raises(runner.RenewalInterruptError):
            runner._terminate_master(master)
    finally:
        signal.signal(signal.SIGTERM, previous)
        if process.poll() is None:
            original(master)
    assert calls == 2
    assert process.poll() is not None


def test_ci_interruption_stops_later_gates_and_exits_130(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real SIGTERM inside a gate must stop scheduling and exit 130."""
    calls: list[str] = []

    def interrupt(*args: object) -> list[JsonObject]:
        calls.append("static")
        os.kill(os.getpid(), signal.SIGTERM)
        return []

    def gate(name: str) -> Callable[..., list[JsonObject]]:
        def run(*args: object) -> list[JsonObject]:
            calls.append(name)
            return [{"command": name, "exit": 0}]

        return run

    monkeypatch.setattr(runner, "_gate_static", interrupt)
    for name in runner.CI_GATES[1:]:
        monkeypatch.setattr(runner, f"_gate_{name.replace('-', '_')}", gate(name))
    artifact_root = _private(tmp_path / "artifacts")
    assert runner.main(["ci", "--artifact-root", str(artifact_root)]) == 130
    assert calls == ["static"]
    report = json.loads((artifact_root / "ci-report.json").read_text())
    assert report["ok"] is False
    assert report["gates"] == {
        "static": [
            {"command": "static", "exit": 130, "error": "renewal runner interrupted"}
        ]
    }


def test_gate_browser_propagates_interruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def interrupted_suite(*args: object) -> JsonObject:
        message = "renewal runner interrupted"
        raise runner.RenewalInterruptError(message)

    monkeypatch.setattr(runner, "_run_browser_suite", interrupted_suite)
    with pytest.raises(runner.RenewalInterruptError):
        runner._gate_browser(REPOSITORY, tmp_path, tmp_path / "run")


def test_stale_or_missing_record_is_rejected(tmp_path: Path) -> None:
    assert (
        runner.main(
            [
                "browser",
                "--suite",
                "smoke",
                "--artifact-root",
                str(tmp_path / "artifacts"),
                "--record",
                str(tmp_path / "absent-record.json"),
            ]
        )
        == 2
    )


def test_runner_module_never_shells_out() -> None:
    source = Path(runner.__file__).read_text(encoding="utf-8")
    assert "shell=True" not in source
    assert "os.system" not in source
    assert "/bin/sh" not in source


def test_renewal_settings_reject_an_owner_role_dsn(tmp_path: Path) -> None:
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/root"),
        "DJANGO_SETTINGS_MODULE": "config.settings.renewal",
        "APP_DATABASE_URL": "postgresql://clinic_owner:pw@127.0.0.1:55432/clinic",
        "SECRET_KEY": "renewal-test-secret",
        "PYTHONPATH": str(REPOSITORY),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    completed = subprocess.run(
        [sys.executable, "-c", "import django; django.setup()"],
        check=False,
        cwd=REPOSITORY,
        env=environment,
        capture_output=True,
        timeout=60,
    )
    assert completed.returncode != 0
    environment["APP_DATABASE_URL"] = (
        "postgresql://clinic_app:pw@127.0.0.1:55432/clinic"
    )
    completed = subprocess.run(
        [sys.executable, "-c", "import django; django.setup()"],
        check=False,
        cwd=REPOSITORY,
        env=environment,
        capture_output=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr.decode()[-500:]


def test_excluded_predicate_matches_the_snapshot_contract() -> None:
    for path in (
        ".omo/evidence/x.json",
        ".venv/bin/python",
        "__pycache__/x.pyc",
        "node_modules/pkg/index.js",
        "evidence/run/out.txt",
        "a/.env",
        "a/.env.local",
        "a/credentials.json",
        "a/id_rsa",
        "a/cert.pem",
        "a/key.key",
        "a/bundle.p12",
        "a/bundle.pfx",
        "a/.env.prod",
        "a/.mypy_cache/x",
        "a/.pytest_cache/x",
        "a/.ruff_cache/x",
    ):
        assert runner._excluded(path) is True, path
    for path in (
        "ops/testing/renewal_runner.py",
        "tests/renewal/browser/test_smoke.py",
        "docs/guide.md",
        ".env.example",
        "README.md",
    ):
        assert runner._excluded(path) is False, path


SECRETS_MODULE = BROWSER_SUITES / "_fixture_secrets.py"
# Reviewed exemptions: file -> reason. The report backstop still redacts
# these values; only the construction layer is missing there.
SECRET_READER_EXEMPTIONS: dict[Path, str] = {
    BROWSER_SUITES / "test_workspace.py": (
        "todo 13 owns the workspace suites and adopts fixture_dsn() at rebase"
    ),
}
# A suite whose fixture dict holds every kind of credential and whose tests
# fail in each way pytest can render one: the argument line, --showlocals,
# a derived plain str, an assertion diff, captured output, an exception
# message, the TOTP seed as bytes and a fixture setup error.
LEAKY_SUITE = """
import json
import os

import pytest

from renewal.browser._fixture_secrets import (
    fixture_dsn, new_access_code, new_password, new_totp_key,
)


@pytest.fixture
def staff():
    values = {
        "dsn": fixture_dsn(),
        "password": new_password(),
        "totp_key": new_totp_key(),
        "code": new_access_code(),
        "clinic": "clinica-sintetica",
    }
    with open(os.environ["LEAK_PROBE_OUT"], "a") as sink:
        sink.write(json.dumps({k: str(v) for k, v in values.items()}) + "\\n")
    return values


def test_argument_line(staff):
    assert staff["clinic"] == "outra"


def test_locals_and_derived_values(staff):
    derived = staff["dsn"].removesuffix("/clinic") + "/other"
    seed = staff["totp_key"][:]
    raise RuntimeError(f"cannot reach {derived} with {seed}")


def test_assertion_diff(staff):
    assert staff["password"] == "typed-" + staff["code"]


def test_captured_output(staff):
    print(staff["dsn"], staff["password"], staff["code"])
    assert staff["totp_key"] in "no seed here"


def test_seed_bytes(staff):
    seed = bytes.fromhex(staff["totp_key"])
    assert seed == b"no seed"


@pytest.fixture
def broken(staff):
    raise RuntimeError(staff["totp_key"] + staff["password"])


def test_setup_error(broken):
    pass
"""


def _leaky_run(
    tmp_path: Path, name: str, *, plugin: bool
) -> tuple[str, str, list[str]]:
    """Run LEAKY_SUITE; return (terminal output, junit xml, secret values)."""
    root = _private(tmp_path / name)
    (root / "test_leaky.py").write_text(LEAKY_SUITE, encoding="utf-8")
    (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    probe = root / "secrets.jsonl"
    owner_password = secrets.token_urlsafe(24)
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": f"{REPOSITORY / 'tests'}{os.pathsep}{REPOSITORY}",
        "PYTHONDONTWRITEBYTECODE": "1",
        "LEAK_PROBE_OUT": str(probe),
        "CLINIC_RENEWAL_FIXTURE_DATABASE_URL": (
            f"postgresql://clinic_owner:{owner_password}@127.0.0.1:5/clinic"
        ),
    }
    junit = root / "junit.xml"
    completed = subprocess.run(  # noqa: S603 - fixed interpreter, closed argv.
        [
            sys.executable,
            "-m",
            "pytest",
            "-c",
            str(root / "pytest.ini"),
            "--rootdir",
            str(root),
            "-p",
            "no:cacheprovider",
            *(["-p", "renewal.browser._fixture_secrets"] if plugin else []),
            "--showlocals",
            "-rA",
            f"--junitxml={junit}",
            str(root / "test_leaky.py"),
        ],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    # Only the summary line: the inner output may carry its synthetic secrets.
    summary = (completed.stdout.strip().splitlines() or [""])[-1]
    assert completed.returncode == 1, summary
    assert "5 failed, 1 error" in summary, summary
    values = [owner_password]
    for line in probe.read_text(encoding="utf-8").splitlines():
        values.extend(v for k, v in json.loads(line).items() if k != "clinic")
        # The seed as bytes.fromhex(...) renders it (test_seed_bytes).
        values.append(repr(bytes.fromhex(json.loads(line)["totp_key"]))[2:-1])
    return (
        completed.stdout + completed.stderr,
        junit.read_text(encoding="utf-8"),
        values,
    )


def test_fixture_secrets_never_reach_a_failure_report(tmp_path: Path) -> None:
    # Hosted retention@firefox 36349167980 printed availability_staff's owner
    # DSN, password included, into pytest.log and the uploaded JUnit file.
    # Construction alone: the argument line and --showlocals show the
    # placeholder, but derived values, diffs and output still leak.
    bare, bare_junit, bare_values = _leaky_run(tmp_path, "bare", plugin=False)
    argument_lines = [line for line in bare.splitlines() if line.startswith("staff = ")]
    assert argument_lines
    # Failure messages name positions only, never a (synthetic) value.
    assert all(
        "'dsn': <fixture secret>" in line and "'password': <fixture secret>" in line
        for line in argument_lines
    ), "argument line renders a fixture secret"
    assert all(value not in "\n".join(argument_lines) for value in bare_values)
    assert any(value in bare or value in bare_junit for value in bare_values)

    # With the report backstop: no value in any rendering, and the placeholder
    # shows where each one was.
    output, junit, values = _leaky_run(tmp_path, "scrubbed", plugin=True)
    rendered = (("terminal", output), ("junit", junit))
    assert [
        index
        for index, value in enumerate(values)
        if any(value in text for _, text in rendered)
    ] == []
    # No piece of FRAGMENT characters of a password, seed or code either:
    # pytest truncates long reprs to head...tail. (A DSN's scheme, user and
    # host are not secret; its password is values[0].)
    pieces = [
        (index, where, start)
        for index, value in enumerate(values)
        if "://" not in value
        for where, text in rendered
        for start in range(len(value) - fixture_secrets.FRAGMENT + 1)
        if value[start : start + fixture_secrets.FRAGMENT] in text
    ]
    assert pieces == []
    assert "clinic_owner:<fixture secret>@127.0.0.1:5/other" in output
    assert "clinic_owner:&lt;fixture secret&gt;@127.0.0.1:5/other" in junit


def test_secret_runner_inputs_are_the_redacted_environment() -> None:
    # Derived from the runner: the suite input carrying the owner password,
    # and every fixture input the runner fills from a DSN.
    sentinel = f"sentinel-{secrets.token_hex(8)}"
    exported = runner._pytest_environment(
        base_url="http://127.0.0.1:1",
        artifact_root=REPOSITORY,
        browser="/usr/bin/true",
        engine="chromium",
        username="user",
        password=sentinel,
    )
    derived = {name for name, value in exported.items() if value == sentinel}
    tree = ast.parse(textwrap.dedent(inspect.getsource(runner._run_browser_suite)))
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if (
                    isinstance(key, ast.Constant)
                    and str(key.value).startswith("CLINIC_RENEWAL_")
                    and "dsn" in ast.unparse(value).lower()
                ):
                    derived.add(str(key.value))
    assert derived == set(fixture_secrets.SECRET_ENVIRONMENT)
    # Suites read them only through _fixture_secrets (FixtureSecret values).
    readers = {
        path: [
            node.lineno
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(node, ast.Constant) and node.value in derived
        ]
        for path in sorted(BROWSER_SUITES.rglob("*.py"))
        if path != SECRETS_MODULE
    }
    # Every exemption is still load-bearing.
    assert all(readers[path] for path in SECRET_READER_EXEMPTIONS)
    assert {
        path: lines
        for path, lines in readers.items()
        if lines and path not in SECRET_READER_EXEMPTIONS
    } == {}
