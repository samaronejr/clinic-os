# Renewal RC — Milestone 1 review (2026-09-24)

Scope: what "green" means for the synthetic renewal release candidate, what
this change adds on top of the renewal head, and which evidence actually ran.
This document is a review aid, not a certification: the renewal PR (#11) was
inspected for the boundaries exercised below; it has not been line-audited in
full.

## Source binding

| item | value |
| --- | --- |
| repository | `samaronejr/clinic-os` |
| verified base (merge-base with `main`) | `e622b2107619cb1af141d86523de2cf883953ff8` |
| renewal head inspected | `3432b985a0c051134f99fe097c9f8a6cc675ed20` (PR #11) |
| hardening worktree branch | `devin/1790227901-renewal-rc-verification` (draft PR #13) |

PR #10 (docs) and PR #12 (test reorganization + CI bootstrap repair) were
inspected and deliberately left out of this work; the test reorganization was
not redone.

## Baseline test-to-gate map (state before this change)

| capability | actual command | environment | required deps | evidence executed? | gap |
| --- | --- | --- | --- | --- | --- |
| unit/integration suite | `uv run pytest --reuse-db … tests` | hosted `test` job | postgres via `ci_postgres.sh`, `memory://` broker | yes (hosted, green on #11/#12) | ran app tests only; no browser journeys |
| coverage | `pytest --cov … --cov-fail-under=90` | same job | identical list duplicated in Makefile + runner | yes | scope list existed twice; both already covered all 14 app modules |
| legacy 3-suite contract | `browser_runner.sh probe` | `contracts` job | chromium probe | yes | executable/image probe + 3 frozen suites only — not the full registered set |
| renewal browser suites | `renewal_runner browser --suite <name>` | **local only** | postgres, gunicorn, Chromium | runner existed; no hosted run | 29 registered suites unbound in CI; no aggregate verdict |
| PDF inspection | `test_document_artifacts.py` poppler checks | `test` job | `pdftotext`/`pdftoppm` | **skipped** — poppler absent | missing tool silently skipped required assertions |
| broker/worker | celery `memory://` transport | `test` job | none | partial | no cross-process delivery, lost-dispatch, or duplicate-credit proof |
| migrations | `test_*_migrations.py`, `makemigrations --check` | `test` job | postgres | yes (fresh install) | no base→candidate upgrade rehearsal with preserved data |
| source authenticity | `isolation_ledger.py snapshot` (first step, every job) | all jobs | frozen plan + sidecar | yes | unchanged |

## Changes by domain and risk

### comms / async execution (functional change — highest risk)

- `enqueue_operation` publishes via `transaction.on_commit`; a broker outage
  at that instant stranded the committed row in `pending` forever.
  **New:** `clinic_app.comms_recover_pending_v1()` (additive migration
  `comms.0004_pending_recovery`, SQL in `_recovery_sql.py`) returns claimable
  rows — `pending` with `not_before NULL` or past, plus `in_progress` rows
  whose claim went stale (>1 min). `comms.recover_pending_operations` runs on
  a 60 s beat. Overlap with the reminder scan is safe: the per-operation
  claim lock deduplicates dispatches.
- `COMMS_SYNTHETIC_FAILURE_CHANNELS` (env-bound in `settings/base.py`)
  injects `TransientSendError` on the named synthetic channel — used only by
  the broker gate; default empty means no faults.
- Celery config note: under `config_from_object(namespace="CELERY")`, broker
  overrides must write `CELERY_BROKER_URL`; plain `broker_url` is silently
  ignored. The gate fixture sets the namespaced key.

### CI / release engineering

- `ci.yml` keeps the frozen bootstrap ordering (checkout → setup-uv →
  snapshot as first `run:` step in every job) and the legacy 3-suite
  `contracts` selector untouched.
- New required gates:
  - `renewal-browser` — 6 shards covering all 29 registered suites
    (`renewal_runner browser --suite …`), real postgres + gunicorn + Chrome,
    per-suite `report.json` + JUnit artifacts always uploaded.
  - `worker-integration` — `apt redis-server` + preflight, then
    `CLINIC_BROKER_GATE=required pytest tests/renewal/test_broker_delivery.py`.
  - `migration-upgrade` — derives the upgrade base as
    `git merge-base origin/main HEAD` (the pre-renewal deployed schema;
    merging against the stacked PR base ref would degenerate to the
    renewal head itself), runs `ops/testing/migration_upgrade.sh`.
  - `renewal-acceptance` — `if: always()`, `needs:` all of the above;
    `ops/testing/renewal_acceptance.py` rejects failed/cancelled/missing
    jobs, absent or malformed suite reports, zero-test suites, shard↔registry
    drift, and divergent `source_manifest_sha256` across shards. Emits the
    stable check name **Renewal RC acceptance**.
- `.omo` binding uses the workspace-symlink contract
  (`ln -sfn $GITHUB_WORKSPACE .omo`); a real directory + `mv` breaks the
  ledger's recorded stable-lock path.
- Action pins extended: `actions/upload-artifact@v4.6.2`,
  `actions/download-artifact@v6.0.0` (SHA-pinned in `action-provenance.json`,
  enforced by `validate_action_pins.py`).

### coverage

- `ops/testing/coverage-targets.txt` is the single authoritative `--cov`
  list (Makefile, runner, and workflow all read it). All 14 app modules are
  in scope: audit, billing, comms, consent, core, ehr, identity, intake,
  interop, prescription, retention, scheduling, teleconsult, tenancy.
  Aggregate floor `--cov-fail-under=90` unchanged; branch coverage is not
  measured and is not claimed.

### PDF

- `poppler-utils` is installed in the `test` job; a preflight runs
  `pdftotext -v`/`pdftoppm -v`. In `_poppler()`, `CLINIC_PDF_TOOLS=required`
  (set in CI) converts the missing-binary skip into a hard failure.
  Documents remain labelled synthetic; nothing here approves a production
  renderer.

### migrations

- `ops/testing/migration_upgrade.sh` + `_seed.py` + `_verify.py`:
  disposable `upgrade_rc_$$` database → `bootstrap.sql` → base migrate →
  representative seed (org/clinic, owner+physician roles, patient, avail,
  appointment, 2 audit events) → `migrate tenancy 0003` → `issue_tenant_key`
  under `app.current_tenant` + synthetic file backend → full candidate
  migrate → verify. Verified: seeded rows preserved, `full_name`/`birth_date`
  ciphertext (no readable plaintext), FORCE RLS on every tenant table,
  `clinic_app` SELECT-yes/DELETE-no on comms operations, cross-tenant zero /
  own-tenant read, audit chain `verify_chain`, and `makemigrations --check`
  drift-free. Product-path seeding runs under `SET ROLE clinic_app` +
  `tenant_context`; audit reads run as owner (app has no audit SELECT).

## Verification executed locally (real dependencies)

| gate | command | outcome |
| --- | --- | --- |
| broker/worker | `CLINIC_BROKER_GATE=required pytest tests/renewal/test_broker_delivery.py` | 9/9 pass — real Redis + separate Celery worker: beat→delivered, rollback non-delivery, lost-dispatch recovery, SIGKILL-restart idempotency, `attempts_exhausted`, forged/duplicate/valid callbacks |
| verdict logic | `pytest tests/renewal/test_acceptance_gate.py` | 20/20 pass (missing job, cancelled, zero-test, skipped, malformed JUnit, missing suite, digest drift all rejected) |
| upgrade | `ops/testing/migration_upgrade.sh e622b210…` | PASS incl. no-drift |
| pins contract | `validate_action_pins.py`, `test_action_pins.py` | pass |
| lint/type | `ruff check`, `ruff format`, `mypy` on touched files | clean |

Hosted evidence: see PR #13 checks (`Renewal RC acceptance` aggregates them).

## Defects the new gate exposed (fixed in this change)

- `attachment_storage._authorize_mutation` claimed the storage ownership
  marker before the attachment root existed; the sibling marker write
  failed with `FileNotFoundError` and every upload returned 503 on a
  fresh environment. The root is now created ahead of the synthetic
  claim (`b3a59d9`).
- `renewal_runner._server_environment` gave suite app servers no broker,
  so request-time `execute_operation_task.apply_async` crashed document
  delivery with kombu `ConnectionRefusedError`. The child now gets
  `CELERY_BROKER_URL=memory://` (in-process only; cross-process delivery
  is still proven exclusively by the Redis broker gate) (`b768984`).
- `current_source_snapshot._open_ledger` did not canonicalize `.omo`,
  which is a symlink in CI; every sibling caller resolves it first
  (`373477d`).

## Known limits / non-claims

- Exactly-once external delivery is **not** claimed; demonstrated behaviour
  is at-most-once per claim under the local idempotency contract plus
  recovery of lost dispatches.
- `Renewal RC acceptance` as a *required* branch check is an owner-side
  ruleset change — pending administrative handoff, not configured here.
- No live-data, real-provider, PIX, or production-renderer approval.
- The containerized HTTPS journey (`image`/`tls` gates) remains separate
  evidence; the renewal browser job serves gunicorn on the runner, not the
  smoke-tested image.
