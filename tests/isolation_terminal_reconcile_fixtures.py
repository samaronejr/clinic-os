from __future__ import annotations

import copy
import os
import stat
from pathlib import Path
from typing import cast

import rfc8785
from ops.testing.isolation_common import (
    MODE_IMMUTABLE,
    JsonObject,
    JsonValue,
    canonical_bytes,
    load_json,
    raw_sha256,
    write_atomic_replace,
    write_no_replace,
)
from ops.testing.isolation_terminal_publisher_authorizations import AUTHORIZATIONS
from ops.testing.isolation_terminal_publisher_journal import RELEASE_KEYS

from isolation_claim_fixtures import SECOND_CLAIM_ID
from isolation_user_fixtures import UserGateFixture, user_gate_fixture


def approved_active_publisher_gate(tmp_path: Path) -> UserGateFixture:
    fixture = user_gate_fixture(tmp_path)
    ledger, _ = load_json(fixture.ledger_path)
    authorizations, observations = _published_terminal_vector(
        fixture.control_root / "terminal"
    )
    claim = _active_publisher_claim(authorizations, observations)
    ledger["claims"] = [claim]
    write_atomic_replace(fixture.ledger_path, canonical_bytes(ledger))
    journal_path = Path(str(ledger["attempt_root"])) / "final-wave-state.json"
    journal, _ = load_json(journal_path)
    journal["terminal_publisher_state"] = "active"
    for key in RELEASE_KEYS:
        journal[key] = None
    spec: JsonObject = {
        "claim_id": claim["claim_id"],
        "dependency_claim_ids": claim["dependency_claim_ids"],
        "desired": claim["desired"],
        "kind": claim["kind"],
        "purpose": claim["purpose"],
    }
    journal["terminal_publisher_reservation_spec_sha256"] = raw_sha256(
        rfc8785.dumps(spec)
    )
    write_atomic_replace(journal_path, canonical_bytes(journal))
    fixture.terminal_path.unlink()
    return fixture


def _published_terminal_vector(
    root: Path,
) -> tuple[list[JsonObject], list[JsonObject]]:
    authorizations: list[JsonObject] = []
    observations: list[JsonObject] = []
    for identifier, predecessors, fixed_paths in AUTHORIZATIONS:
        paths = list(fixed_paths or ("F3/runtime.json",))
        authorization: JsonObject = {
            "authorization_id": identifier,
            "gid": os.getegid(),
            "governing_lock": "final",
            "mode": MODE_IMMUTABLE,
            "output_kind": "final-terminal",
            "predecessor_authorization_ids": list(predecessors),
            "relative_paths": cast("JsonValue", paths),
            "root_path": str(root),
            "uid": os.geteuid(),
        }
        entries = [_published_entry(root, identifier, relative) for relative in paths]
        observation: JsonObject = {
            "authorization_id": identifier,
            "entries": cast("JsonValue", entries),
            "governing_lock": "final",
            "output_kind": "final-terminal",
            "root_path": str(root),
            "status": "published",
        }
        authorizations.append(authorization)
        observations.append(observation)
    return authorizations, observations


def _published_entry(root: Path, identifier: str, relative: str) -> JsonObject:
    path = root / relative
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.exists():
        write_no_replace(
            path,
            f"{identifier}:{relative}\n".encode(),
            mode=MODE_IMMUTABLE,
        )
    value = path.stat(follow_symlinks=False)
    raw = path.read_bytes()
    return {
        "gid": value.st_gid,
        "mode": stat.S_IMODE(value.st_mode),
        "relative_path": relative,
        "sha256": raw_sha256(raw),
        "size_bytes": len(raw),
        "uid": value.st_uid,
    }


def _active_publisher_claim(
    authorizations: list[JsonObject],
    observations: list[JsonObject],
) -> JsonObject:
    return {
        "activated_at_utc": "2026-07-16T23:00:02.000003Z",
        "candidate_envelope_binding": None,
        "claim_id": SECOND_CLAIM_ID,
        "dependency_claim_ids": [],
        "desired": {
            "owned_files": [],
            "published_outputs": cast("JsonValue", copy.deepcopy(authorizations)),
        },
        "kind": "filesystem",
        "last_verified_at_utc": "2026-07-16T23:00:03.000004Z",
        "observed": {
            "owned_files": [],
            "published_outputs": cast("JsonValue", copy.deepcopy(observations)),
        },
        "prepared_at_utc": None,
        "purpose": "final-terminal-publisher",
        "reserved_at_utc": "2026-07-16T23:00:01.000002Z",
        "root_relative_path": f"claims/{SECOND_CLAIM_ID}",
        "runner_creation": None,
        "status": "active",
    }
