from __future__ import annotations

from pathlib import Path

from ops.testing.isolation_common import JsonObject, JsonValue, load_json
from ops.testing.isolation_ledger_store import locked_open_ledger

from browser.browser_settings_claim_builders import (
    active_filesystem,
    active_stack,
    add_materializer_volume,
    reserved_process,
)
from browser.browser_settings_claim_values import (
    CA_EXPORT_ID,
    DATABASE,
    DATABASE_ID,
    GUNICORN_CONFIG,
    MATERIALIZER_ID,
    PROCESS_ID,
    PROCESS_PORT,
    PROJECT,
    browser_environment,
    claim_root,
    object_value,
    sha,
    text,
)
from isolation_claim_fixtures import snapshot


def browser_ledger_fixture(tmp_path: Path) -> tuple[Path, JsonObject, JsonObject]:
    ledger_path = snapshot(tmp_path)
    ledger, _ = load_json(ledger_path)
    timestamp = text(ledger["last_verified_at_utc"])
    attempt_id = text(ledger["attempt_id"])
    attempt_root = Path(text(ledger["attempt_root"]))
    claims_root = attempt_root / "claims"
    claims_root.mkdir(mode=0o700)

    materializer = active_stack(MATERIALIZER_ID, "tls-materializer", 15431, timestamp)
    add_materializer_volume(materializer, tmp_path)
    database = active_stack(DATABASE_ID, "browser-database", 15432, timestamp)
    database["dependency_claim_ids"] = [MATERIALIZER_ID]
    database_desired = object_value(database["desired"])
    database_desired["project"] = PROJECT
    database_desired["database_names"] = [DATABASE]

    ca_root = claim_root(claims_root, CA_EXPORT_ID)
    ca_path = ca_root / "db-ca.pem"
    ca_path.write_bytes(b"synthetic browser CA fixture\n")
    ca_path.chmod(0o600)
    ca_identity = ca_path.stat(follow_symlinks=False)
    ca_observed: JsonObject = {
        "device": ca_identity.st_dev,
        "gid": ca_identity.st_gid,
        "inode": ca_identity.st_ino,
        "mode": 0o600,
        "relative_path": "db-ca.pem",
        "sha256": sha(ca_path.read_bytes()),
        "uid": ca_identity.st_uid,
    }
    ca_export = active_filesystem(timestamp, ca_observed)

    process_root = claim_root(claims_root, PROCESS_ID)
    config = tmp_path / "ops" / "container" / "gunicorn_no_proxy.py"
    config.parent.mkdir(parents=True)
    config.write_bytes(GUNICORN_CONFIG)
    launcher = Path(__file__).resolve().parents[2] / ".venv/bin/python"
    environment = browser_environment(attempt_id, process_root / "ledger-rpc.sock")
    process = reserved_process(
        timestamp,
        launcher,
        config,
        environment,
        ca_observed,
    )
    claims: list[JsonValue] = [materializer, database, ca_export, process]
    with locked_open_ledger(ledger_path) as session:
        session.ledger["claims"] = claims
        session.commit()
    ledger, _ = load_json(ledger_path)
    expected: JsonObject = {
        "attempt_id": attempt_id,
        "ca_export_claim_id": CA_EXPORT_ID,
        "database_claim_id": DATABASE_ID,
        "database_name": DATABASE,
        "database_port": 15432,
        "environment_contract": environment,
        "foundation_sha": ledger["foundation_sha"],
        "materializer_claim_id": MATERIALIZER_ID,
        "process_claim_id": PROCESS_ID,
        "process_port": PROCESS_PORT,
        "project": PROJECT,
        "worktree_realpath": ledger["worktree_realpath"],
    }
    return ledger_path, ledger, expected
