# ISOLATION AND EVIDENCE HARNESS

## OVERVIEW
Executable resource-isolation, browser, image, TLS, restore, and review-evidence tooling; pytest cases live in `tests/`.
Scope score: 15; distinct harness domain with local container configuration, schemas, and highly referenced primitives.

## WHERE TO LOOK
| Task | Location | Notes |
|------|----------|-------|
| Ledger CLI grammar | `isolation_ledger.py` | Snapshot, claims, runners, reconcile, archive/close dispatch |
| Canonical records and durable I/O | `isolation_common.py` | JSON types, `IsolationError`, locks, inode identities |
| Serialized ledger access | `isolation_ledger_store.py` | `locked_open_ledger` boundary |
| Approved snapshot | `isolation_snapshot*.py`, `isolation_tracked_snapshot*.py` | Local and tracked-CI authority paths |
| Host proof | `execution_host_preflight.py`, `cgroup_*.py` | Host identity and capability evidence |
| Claim lifecycle | `isolation_claim_transitions.py`, `isolation_refresh.py` | Reserve/activate/release and refresh verification |
| Recovery | `isolation_same_boot_coordinator.py`, `isolation_stale_*.py` | Separate same-boot and stale-boot paths |
| Database lifecycle | `isolated_db.sh`, `ci_postgres*.py`, `ci_postgres.sh` | Claimed Compose resources and CI environment |
| Browser orchestration | `browser_runner.sh`, `browser_runner_controller.py`, `browser_session*.py` | Probe and session paths |
| Browser journeys | `browser_suites/AGENTS.md` | Suite-specific guidance |
| Image/TLS contracts | `image_smoke.sh`, `https_*.py`, `tls_*.py` | Candidate build and HTTPS probes |
| Restore rehearsal | `restore_rehearsal.py`, `restore_*.py` | Transport, fixture, verification, acceptance |
| Final evidence | `final_*.py`, `isolation_final_*.py`, `isolation_terminal_*.py` | Frozen inputs, controllers, publication |
| Review receipts | `run_review_gate.sh`, `todo_receipt_*.py`, `validate_*receipt.py` | Review execution and receipt validation |
| Hosted action provenance | `validate_action_pins.py`, `action-provenance.json` | Closed action allowlist and snapshot ordering |

## CONVENTIONS
- Prefixes are domain boundaries in a mostly flat package; filenames distinguish controller, records, validation, and effects.
- Ledger failures use `IsolationError`; `run_cli` prints the prefixed reason and returns 2.
- `isolation_common.canonical_bytes` sorts keys, removes separator whitespace, retains Unicode, and appends one newline.
- `load_json` requires those exact canonical bytes and an object root; reads are capped at 16 MiB.
- Runtime browser framing separately uses RFC 8785 in `browser_runtime_dispatch.py`; preserve each protocol's encoder.
- Permission modes are private `0600`, immutable publication `0400`, and directories `0700`.
- Stable locks verify the pathname and open descriptor identify the same inode before and after the critical section.
- New publication uses `write_no_replace`; mutable state uses same-directory durable replacement.
- Co-located JSON schemas describe machine-consumed evidence; normative vectors live in `tests/fixtures/isolation/normative/`.
- Direct ledger execution adds the project root to `sys.path`; its E402 exceptions serve standalone execution.
- Browser wrappers clear inherited environment with `env -i` and use an absolute project-environment Python path.
- Required browser suite arguments are sorted, unique, and restricted to the closed suite set.

## ANTI-PATTERNS
- Do not substitute ordinary JSON writes for canonical record publication or remove directory fsyncs.
- Do not replace a stable lock inode while held, follow record symlinks, or bypass ownership/mode checks.
- Do not add CLI grammar aliases without changing the closed dispatcher contract.
- Do not silently delete claimed resources after failed database startup; `isolated_db.sh` retains them for journaled cleanup.
- Isolated PostgreSQL must bind IPv4 loopback; host port 5432 is rejected.
- Do not declare GitHub Actions service containers: the provenance validator forbids `services:`.
- Action SHA updates must stay consistent with the allowlist, version comment, and provenance record.

## RELATED CHECKS
- `tests/test_isolation_*.py`: ledger transitions, recovery, publication, schemas, and CLI behavior.
- `tests/test_browser_runner_contract.py`: suite selection and runner contract.
- `tests/test_isolation_normative_schemas.py`: normative evidence vectors.
- `tests/test_browser_server_contract.py`: server-to-browser integration contract.
