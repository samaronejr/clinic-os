"""Validate the filesystem capability used to publish one todo receipt."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Final, Never, cast

from ops.testing.isolation_common import (
    MODE_IMMUTABLE,
    IsolationError,
    JsonObject,
    JsonValue,
)

AUTHORIZATION_KEYS: Final = frozenset(
    {
        "authorization_id",
        "output_kind",
        "root_path",
        "governing_lock",
        "predecessor_authorization_ids",
        "relative_paths",
        "mode",
        "uid",
        "gid",
    }
)
DESTINATION_NAME: Final = re.compile(
    r"^task-(?:[1-9]|1[0-9]|20)-clinic-os-phase-1a-staff-scheduling\.json$"
)


def validate_todo_receipt_desired(
    desired: JsonObject,
    attempt_root: Path | None,
) -> None:
    """Require one exact stable-lock publication capability for one receipt."""
    owned = _objects(desired.get("owned_files"), "todo owned files")
    outputs = _objects(desired.get("published_outputs"), "todo authorizations")
    if not outputs:
        return
    if len(owned) != 1 or len(outputs) != 1:
        _fail("todo receipt claim must own and authorize exactly one file")
    authorization = outputs[0]
    if set(authorization) != AUTHORIZATION_KEYS:
        _fail("todo receipt authorization has the wrong closed key set")
    relative_paths = authorization.get("relative_paths")
    if (
        authorization.get("authorization_id") != "todo-receipt"
        or authorization.get("output_kind") != "todo-evidence"
        or authorization.get("governing_lock") != "stable"
        or authorization.get("predecessor_authorization_ids") != []
        or authorization.get("mode") != MODE_IMMUTABLE
        or authorization.get("uid") != os.geteuid()
        or authorization.get("gid") != os.getegid()
        or not isinstance(relative_paths, list)
        or len(relative_paths) != 1
        or not isinstance(relative_paths[0], str)
        or DESTINATION_NAME.fullmatch(relative_paths[0]) is None
    ):
        _fail("todo receipt authorization does not match the closed capability")
    root = authorization.get("root_path")
    if not isinstance(root, str) or not Path(root).is_absolute():
        _fail("todo receipt authorization root is not absolute")
    if attempt_root is not None and Path(root) != attempt_root / "todo-evidence":
        _fail("todo receipt authorization root escaped the current attempt")


def _objects(value: JsonValue | None, context: str) -> list[JsonObject]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        _fail(f"{context} must be an object array")
    return cast("list[JsonObject]", value)


def _fail(message: str) -> Never:
    raise IsolationError(message)
