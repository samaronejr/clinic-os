"""Generate only todo 57 metadata from the repository's discovery functions."""

import json
import re
import sys
from pathlib import Path

import django

LANE = Path("/home/samarone/Documents/clinic-ops-new-roadmap/clinic-os-wt/cpi-e-t57-r2")
E = LANE / ".omo/evidence/clinic-ops-premium-intelligence/task-57/takeover"
sys.path[:0] = [str(LANE), str(LANE / "tests")]
django.setup()

from billing.psp_oracles import DELEGATED, OBSERVED, PROVIDER, SQL_PROTOCOL_ORACLES
from identity.legacy_guard_inventory import declared_probes, discover, discover_sql
from infra.test_recovery_manifest import catalog_relations, stale_entries, unclassified
from ops.testing import restore_contract

inventory = LANE / "tests/identity/legacy_guards.json"
data = json.loads(inventory.read_text())
prior = {row["symbol"]: row for row in data["candidates"]}
python = discover()
owned = set(DELEGATED) | set(OBSERVED) | set(PROVIDER)
assert owned == {symbol for symbol in python if symbol.startswith("apps.billing.psp_")}
assert not (set(python) - set(prior) - owned), "corrected todo 56 census is required"
assert not (set(prior) - set(python)), "a pre-existing candidate disappeared"
for symbol, signals in python.items():
    if symbol not in owned:
        assert prior[symbol]["signals"] == signals, (
            "refusing another lane's discovery drift", symbol
        )
        continue
    row = {"symbol": symbol, "signals": signals}
    if symbol in DELEGATED:
        row.update(
            kind="delegated",
            enforced_by=["apps.billing.security.financial_actor"],
            reason=(
                "The scoped financial writer reaches the exact permission guard "
                "exercised by the journal's executable financial-actor oracle."
            ),
        )
    elif symbol in OBSERVED:
        row.update(
            kind="direct", observe=True, probes=[symbol + "#observed"],
            reason=(
                "The real patient or worker callable is entered and its SQL/GUC "
                "authority channel is observed; native PSP tests cover valid "
                "effects, role denials, current sessions and opaque unknowns."
            ),
        )
    else:
        row.update(
            kind="provider",
            reason=(
                "Recorded-provider eligibility or authenticated stored-scope "
                "recording bootstrap, with no caller staff-role decision. "
                "Raw-byte authentication precedes scope resolution; provider "
                "and outbox regressions execute this boundary."
            ),
        )
    prior[symbol] = row
data["candidates"] = [prior[name] for name in sorted(prior)]
data["probes"] = sorted(
    set(data["probes"]) | declared_probes() | {name + "#observed" for name in OBSERVED}
)
sql = discover_sql()
new_sql = set(sql) - set(data["sql_guards"])
assert all(name.startswith("clinic_app.billing_psp_") or name ==
           "clinic_app.billing_patient_psp_instructions" for name in new_sql), (
    "refusing another lane's new SQL roots", new_sql
)
assert not (set(data["sql_guards"]) - set(sql))
for name, discovery in sql.items():
    if name in data["sql_guards"]:
        # PSP callers legitimately extend existing resolver dependency sets.
        data["sql_guards"][name]["discovery"] = discovery
    elif name in SQL_PROTOCOL_ORACLES:
        data["sql_guards"][name] = {
            "discovery": discovery,
            "kind": "domain_protocol",
            "probes": SQL_PROTOCOL_ORACLES[name],
            "reason": (
                "Immutable PSP requests bind stored actors, exact clinic "
                "authority and fresh outgoing sessions. The named native "
                "oracles exercise the actual guards and lookup recovery."
            ),
        }
    else:
        patient = name == "clinic_app.billing_patient_psp_instructions"
        data["sql_guards"][name] = {
            "discovery": discovery,
            "kind": "patient_principal" if patient else "worker_protocol",
            "probes": [],
            "reason": (
                "Released-invoice patient/delegate authority is revalidated by "
                "the canonical billing patient resolver and native PSP tests."
                if patient else
                "Stored provider/reference scope and event deduplication confer "
                "no staff-role decision; authenticated callbacks resolve these "
                "only after exact raw-byte authentication."
            ),
        }
data["sql_guards"] = {name: data["sql_guards"][name] for name in sorted(sql)}
generated = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
(E / "legacy_guards.generated.json").write_text(generated)

relations = catalog_relations()
assert not unclassified(relations) and not stale_entries(relations)
domain = {
    item.name for item in relations
    if item.schema == "clinic_app" and item.kind in {"r", "p"}
    and item.name not in restore_contract.EXCLUDED_RELATIONS
}
assert domain == set(restore_contract.DOMAIN_RELATIONS)
psp_tables = sorted(name for name in domain if name.startswith("billing_psp"))
contract_path = LANE / "ops/testing/restore_contract.py"
contract = contract_path.read_text()
block = "".join(f'    "{name}",\n' for name in psp_tables)
contract, replacements = re.subn(
    r'(?:    "billing_psp[a-z]+",\n)+', block, contract, count=1
)
assert replacements == 1
(E / "restore_contract.generated.py").write_text(contract)
pin_path = LANE / "tests/isolation/test_backup_restore_guard.py"
pin = pin_path.read_text()
pin, replacements = re.subn(
    r"assert len\(restore_contract\.DOMAIN_RELATIONS\) == \d+",
    f"assert len(restore_contract.DOMAIN_RELATIONS) == {len(domain)}",
    pin, count=1,
)
assert replacements == 1
(E / "backup_restore_guard.generated.py").write_text(pin)
(E / "discovery-receipt.json").write_text(json.dumps({
    "python_discovery": "identity.legacy_guard_inventory.discover",
    "sql_discovery": "identity.legacy_guard_inventory.discover_sql",
    "relation_discovery": "infra.test_recovery_manifest.catalog_relations",
    "owned_python_candidates": sorted(owned),
    "new_sql_roots": sorted(new_sql),
    "derived_psp_relations": psp_tables,
    "derived_domain_count": len(domain),
    "unclassified": unclassified(relations),
    "stale_entries": stale_entries(relations),
}, indent=2) + "\n")
print("PSP_DISCOVERY_GENERATED", len(owned), len(new_sql), len(domain), flush=True)
