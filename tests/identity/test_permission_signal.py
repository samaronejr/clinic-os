"""The permission_helper census signal resolves spellings; it fails closed."""

from __future__ import annotations

import ast
import functools
import importlib
import re
from typing import TYPE_CHECKING

import pytest
from apps.identity import current_context

from identity import legacy_guard_inventory
from identity.legacy_guard_inventory import (
    PERMISSION_HELPER,
    SIGNALS,
    discover,
    permission_aliases,
)

if TYPE_CHECKING:
    from pathlib import Path

HEADER = "from apps.identity import current_context\n"
# Each spelling reaches has_permission through a name other than the helper's.
PLANTED = {
    "import_alias": (
        "from apps.identity.current_context import require_permission as check\n"
        "def guard(clinic):\n    check('x', clinic_id=clinic)\n"
    ),
    "assigned_alias": (
        "from apps.identity.current_context import require_permission\n"
        "gate = require_permission\n"
        "def guard(clinic):\n    gate('x', clinic_id=clinic)\n"
    ),
    "chained_alias": (
        "from apps.identity.current_context import require_permission as rp\n"
        "first = rp\nsecond: object = first\n"
        "def guard(clinic):\n    second('x', clinic_id=clinic)\n"
    ),
    "partial_alias": (
        "from functools import partial\n"
        "from apps.identity.current_context import require_permission as rp\n"
        "read = partial(rp, 'demographics.read')\n"
        "def guard(clinic):\n    read(clinic_id=clinic)\n"
    ),
    "getattr_string": (
        HEADER + "def guard(clinic):\n"
        "    getattr(current_context, 'require_permission')('x', clinic_id=clinic)\n"
    ),
    "module_attribute": (
        HEADER + "def guard(clinic):\n"
        "    current_context.require_permission('x', clinic_id=clinic)\n"
    ),
    "walrus_alias": (
        "from apps.identity.current_context import require_permission\n"
        "def guard(clinic):\n"
        "    if (gate := require_permission):\n"
        "        gate('x', clinic_id=clinic)\n"
    ),
}
RESULT_ONLY = (
    "from apps.identity.current_context import require_permission\n"
    "actor = None\n"
    "def guard(clinic):\n    global actor\n"
    "    actor = require_permission('x', clinic_id=clinic)\n"
    "def reader():\n    return actor\n"
)


def _plant(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str) -> None:
    module = tmp_path / "apps/planted/services.py"
    module.parent.mkdir(parents=True)
    module.write_text(source)
    monkeypatch.setattr(legacy_guard_inventory, "ROOT", tmp_path)


@pytest.mark.parametrize("spelling", sorted(PLANTED))
def test_every_spelling_of_the_helper_is_a_permission_site(
    spelling: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plant(tmp_path, monkeypatch, PLANTED[spelling])
    found = discover()
    assert "permission_helper" in found["apps.planted.services.guard"]
    if spelling not in {"getattr_string", "module_attribute"}:
        # The literal call regex alone misses it: the derived alias set is
        # what catches this spelling.
        body = ast.unparse(ast.parse(PLANTED[spelling]))
        guard = body[body.index("def guard") :]
        assert not re.search(SIGNALS["permission_helper"], guard)


def test_a_stored_result_is_not_an_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plant(tmp_path, monkeypatch, RESULT_ONLY)
    found = discover()
    assert "permission_helper" in found["apps.planted.services.guard"]
    assert "apps.planted.services.reader" not in found
    assert permission_aliases(ast.parse(RESULT_ONLY)) == {PERMISSION_HELPER}


def test_runtime_bindings_are_all_derived_spellings() -> None:
    """Cross-check the static aliases against every imported apps module."""
    root = legacy_guard_inventory.ROOT
    checked = 0
    for path in sorted((root / "apps").rglob("*.py")):
        if "migrations" in path.parts:
            continue
        name = str(path.relative_to(root))[:-3].replace("/", ".")
        module = importlib.import_module(name.removesuffix(".__init__"))
        aliases = permission_aliases(ast.parse(path.read_text()))
        for attribute, value in vars(module).items():
            target = value.func if isinstance(value, functools.partial) else value
            if target is current_context.require_permission:
                checked += 1
                assert attribute in aliases, (name, attribute)
    # Every direct importer binds the helper; the cross-check is not vacuous.
    assert checked >= 5
