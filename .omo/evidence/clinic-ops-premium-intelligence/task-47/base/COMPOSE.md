# Todo 47 Wave C base composition

Date: 2026-10-28.
Branch: `ulw/cpi-c-t47-base`.
Worktree: `/home/samarone/Documents/clinic-ops-new-roadmap/clinic-os-wt/cpi-c-t47-base`.
Verified source head: `5cc0eab90e69274e52dec5b8b4c88429b22e8cb1`.
The following evidence-only commit records this document; it does not change
the verified Python, migrations, templates, static assets, or catalogues.

## Result and scope

All requested approved Wave C lanes, including t42, are composed into a
source-only base.
Ruff, formatting, strict uncached mypy, and database-free migration
autodetection pass. Disk migration loading reports no conflicts and one leaf
per migrated app. No database connection, database creation, Docker, pytest,
browser, systemd operation, or push was performed.

The original main worktree and all lane branches were left unchanged.
Its pre-existing untracked `pid-before1` was not touched.

**T42 SLOT FILLED:** Root subsequently supplied
`bfde9a07e5d01eac337f0adecf71004d06ffc9b3`. It was added as the last lane
pick, after the pre-t42 graph repair. Its conflicting scribe migration number
was reconciled to `0005_note_drafts` after `0004_purge_state`.
This is the complete requested source composition, not the todo 47
integrated runtime proof.

## Ancestry and selected commits

Read the precedent at
`ulw/cpi-c-integration-dryrun` (`38d8915`), specifically
`.omo/evidence/clinic-ops-premium-intelligence/wave-c-integration-dryrun/REPORT.md`.
Its identity constraint incompatibility and multiple-leaf warnings were
treated as unresolved integration work, not runtime approval.

The new worktree starts at `f5913985f3bceea7b12e203d34215daea8928252`,
the preferred replay of t41 on `90671ec8`. That trunk already contains,
in order:

| Task | Original approved source | Retained/replayed trunk commit |
| --- | --- | --- |
| 38 | `c89c76c5debee4d19f549a3980860167aefeed6f` | unchanged |
| 39 | `9e2bdc5f1d1062575d1a041deccd998d43cec3e4` | unchanged |
| 44 | `98995a1ae73fabaadd77a290923b9776866f5a27` | unchanged |
| 49 | `89f5bf630a9ddcc9dbe43bf1d4095f414376c74e` | unchanged |
| 40 | `02e549af026cf52c8377fcb9e0000ad663698ceb` | `90671ec8f3e46b0447732e55995e71c3f7580ff7` |
| 41 | `1a1d539b2e3b15f291d640c02cccc6c3bb1832c4` | `f5913985f3bceea7b12e203d34215daea8928252` |

`git cherry f591398 <lane-head>` returned exactly one positive commit for
each of t43-on39, t46, and t45. Only those lane-owned commits were picked.
`git merge-base --is-ancestor 98995a1 HEAD` succeeded, so t44 was not
duplicated. New commits are:

| Task | Picked source | New scratch SHA |
| --- | --- | --- |
| 43-on39 | `63fbea06208fb8967e3b9dd26c57d77c8d30dbbd` | `195339ad3b8452d4017605f677c7be95256a02f0` |
| 46 | `a977c92d24bad4edb492fa47f6e465b04b6e0aee` | `bd58eb6c8ec8af960dd1261ad49331503f137a76` |
| 45 | `84f52f4bbd3beca6d766f49ca230c74c6c9cb7b1` | `96bb2aec2fd97f9c9c3ccff1cbb738aad6ab3dca` |
| Composition repair | new, not a lane replay | `e5a0f962c2218c9962612abe8dc9d4a4d5291dac` |
| 42, last lane pick | `bfde9a07e5d01eac337f0adecf71004d06ffc9b3` | `b3ee207b08951d5a9284a636ba2b13bf60b97a2a` |
| Combined refusal-helper repair | new, not a lane replay | `5cc0eab90e69274e52dec5b8b4c88429b22e8cb1` |

Scratch cherry-picks and commits use `core.hooksPath=/dev/null` to avoid
invoking runtime gates outside this source-only authorization.

## Every textual and binary conflict

### t43-on39 replay

* `apps/scribe/outbox.py`: retain t41 gateway imports, recording session,
  and transcription dispatch; add retention's `media_location` import.
  The rest of both outbox changes text-merged.
* `locale/pt_BR/LC_MESSAGES/django.po`: retain both the transcript and
  retention message blocks.
* `locale/pt_BR/LC_MESSAGES/django.mo`: regenerate from the resolved PO,
  never choose one lane's compiled catalogue.
* `tests/conftest.py`: retain all three transcript truncate guards and
  the purge-tombstone truncate guard.
* `tests/identity/legacy_guards.json`: union the scribe-assignment resolver
  discovery list, retaining ASR/coverage/transcript guards and adding
  `retention_purge_visible`; deduplicate the shared `scribe_guard`.
* `tests/isolation/test_backup_restore_guard.py`: retain the explicit
  source-union-only warning and set the intermediate relation count to 170
  (169 from the t41 trunk plus the purge relation).
* `tests/teleconsult/test_mode_migration.py`: preserve t41's complete
  irreversible-rollback refusal and schema-editor replay of the reversible
  mode migrations, with restoration in `finally` and unchanged migration
  recorder assertion. Restore its `OLD` target, removed by the conflicting
  patch, and remove the now-unused alternative retention replay helper.
  This keeps mode reversal isolated from protected transcript destruction.

### t46 replay

* `locale/pt_BR/LC_MESSAGES/django.po`: retain transcript/retention messages
  and add the full decision-support block.
* `locale/pt_BR/LC_MESSAGES/django.mo`: regenerate from the combined PO.
* `tests/core/test_workspace_in_view_refusals.py`: keep separate request
  populations for capture control and decision support, including both
  native and HTMX cases.

The shared B1/SC-15 correction in `apps/ai/output_validation.py` was applied
without a conflict. Parsed `request_schema` identity still selects the
brief and decision-support gateway validation contracts.

### t45 replay

These conflicts are additive; all additions from both lanes remain:

* `apps/ai/README.md`: retain the chart-assistant contract and append the
  extraction contract; the earlier decision-support section remains.
* `apps/ai/adapters.py`: retain decision-support imports and add extraction
  exception/document adapter imports.
* `apps/ai/urls.py`: retain chart-assistant and decision-support imports and
  routes and add the extraction import and route.
* `apps/audit/events.py`: retain decision-support decision events and add
  all four extraction events.
* `tests/audit/test_phase1_audit_contract.py`: retain both event vocabularies.
* `tests/identity/test_sql_inactive_actor.py`: retain retention protocol
  oracles and add extraction protocol oracles.
* `tests/infra/test_module_boundaries.py`: retain retention's exact service
  signature and add the three extraction service signatures.
* `tests/infra/test_resolver_catalog.py`: retain retention column grants
  and add extraction-candidate review column grants.
* `tests/isolation/test_backup_restore_guard.py`: keep the source-union
  warning and use the pre-t42 source-list length of 172, including both new
  extraction relations.
* `locale/pt_BR/LC_MESSAGES/django.mo`: regenerate from the auto-merged PO,
  retaining all lanes' translations.

### t42 final replay

`git cherry HEAD bfde9a07` returned only the approved t42 commit.
These five paths conflicted:

* `templates/ehr/encounter.html`: load both `ai_decision` and
  `scribe_review` alongside the existing libraries, preserving both panels
  and their auto-merged call sites.
* `tests/conftest.py`: retain both extraction truncate guards and add all
  four draft/provenance guards.
* `tests/isolation/test_backup_restore_guard.py`: preserve the
  source-union-only warning instead of carrying over t42's lane-specific
  live-catalogue claim; the composed source list is 176 unique relations.
* `tests/scheduling/clock_catalog_inventory.json`: retain all existing
  AI constraint nodes. The two incoming provider `agent_send_mutation`
  nodes already exist, with identical bodies, at their sorted trigger
  positions; retain those once rather than insert duplicates at the top.
  Other t42 clock additions auto-merged. Validate the resulting JSON.
* `locale/pt_BR/LC_MESSAGES/django.mo`: regenerate from the complete
  auto-merged PO.

Renumber t42's `scribe.0004_note_drafts` to `0005_note_drafts` and depend on
`0004_purge_state`. Update the scribe README identifier. Other t42 migration
dependencies remain unchanged.

The combined `_probe_route` in
`tests/core/test_workspace_in_view_refusals.py` reaches Ruff complexity 11
after all lanes add their probes (limit 10). Replace the decision-support
outer conditional with the same route filter in its request comprehension.
Both native/HTMX probes and all t42 review probes remain; no rules or tests
were suppressed. Commit this separately as the narrow integration repair
listed above.

## Migration reconciliation

The repaired t43-on39 source already orders
`identity.0040_agent_execution_authority -> 0041_retention_authority`.
Its constraints preserve versions `(1, 2, 3)` and the clinic-scoped pairs
`1/appointment.read`, `2/agent.execute`, and `3/retention.execute`.
No extra identity rewrite or empty merge migration was needed.
Likewise, its AI retention migration already depends on agent execution.

Two numeric collisions remained after adding t41 and t45:

| App | Later migration rename | New preceding dependency |
| --- | --- | --- |
| ai | `0003_document_extraction` -> `0004_document_extraction` | `0003_artifact_retention` |
| scribe | `0003_purge_state` -> `0004_purge_state` | `0003_transcripts` |
| scribe, t42 last | `0004_note_drafts` -> `0005_note_drafts` | `0004_purge_state` |

Update `retention.0005_transient_purge` to depend on
`scribe.0004_purge_state`. Update the disk migration lookup in
`tests/ai/test_extraction_state.py` and the migration identifier in the AI
README. No remaining old-name references were found.

The resulting app chains are:

* AI: `0001_initial -> 0002_agent_execution -> 0003_artifact_retention ->
  0004_document_extraction`.
* Scribe: `0001_initial -> 0002_ai_disclosure_capture -> 0003_transcripts ->
  0004_purge_state -> 0005_note_drafts`.
* Identity ends at `0041_retention_authority`.
* Retention ends at `0005_transient_purge`, with the updated cross-app edge.

Disk `MigrationLoader(None)` reports `detect_conflicts() == {}` and a single
leaf for every app in its graph. This is an unshipped source-graph repair,
not evidence of a safe upgrade for any previously applied alternate history.

## Static results

Python 3.12.3 and Django 5.2.17. Commands ran from the new worktree.
Use `uv run --frozen --no-sync --no-env-file` for each command below.
The complete existing environment was selected read-only with
`UV_PROJECT_ENVIRONMENT=../cpi-c-integration-dryrun/.venv`
(resolved to its absolute path in the actual calls).

| Check | Result |
| --- | --- |
| `ruff check .` | Exit 0, all checks passed |
| `ruff format --check .` | Exit 0, 2110 files already formatted |
| `mypy --no-incremental --cache-dir=/dev/null .` | Exit 0, no issues in 2110 source files |
| `import django` | Exit 0, version 5.2.17 |
| Import recovery contract | Exit 0, 176 relations and 176 unique names |
| `makemigrations --check --dry-run`, database-disabled wrapper below | Exit 0, no changes detected |
| Disk `MigrationLoader(None)` | Exit 0, no conflicting migration leaves |
| Compile `ehr/encounter.html` through Django's real template loader | Exit 0, both combined template-tag libraries resolve |
| `python -m json.tool tests/scheduling/clock_catalog_inventory.json /dev/null` | Exit 0 |
| `git diff --check` / staged equivalent | Exit 0 |
| `msgfmt --check -o /dev/null .../django.po` | Exit 0; existing missing PO metadata header warnings only |

To guarantee no database connection, migration autodetection ran through the
real `manage.py` entrypoint with the test settings and a dummy backend:

```python
import runpy
import sys
from django.conf import settings

settings.DATABASES = {"default": {"ENGINE": "django.db.backends.dummy"}}
sys.argv = ["manage.py", "makemigrations", "--check", "--dry-run"]
runpy.run_path("manage.py", run_name="__main__")
```

This preserves model/state autodetection while deliberately excluding
database recorder consistency and database system checks.

Initial mypy and settings-import attempts using the main worktree's incomplete
environment failed: the plugin could not initialize and `opentelemetry` was
missing. Retrying with the precedent worktree's complete locked environment
passed without source changes, installation, or dependency changes.
The editor LSP likewise reported unresolved `django.db`; the complete
environment's strict mypy result is the verified typing evidence.

The pre-t42 pass covered 2088 files. After adding t42, repeat the full static
checks: full strict mypy passes on 2110 files, and migration autodetection
again reports no changes. The first combined Ruff attempt exposes the new
complexity violation described above; after its equivalent route-filter
refactor, Ruff and formatting pass on all 2110 files. Run an additional
no-cache mypy check of that changed helper to cover its final form.

## Runtime proof still required

* Re-derive the complete recovery manifest from one migrated catalogue.
  The count 176 is a unique source-list union, not newly observed PostgreSQL
  evidence. Verify classifications, target-owned relations, required-empty
  sets, sequence ownership, protected ciphertext, and purge tombstone replay.
* Re-derive and verify exact resolver signatures, grants, return types,
  volatility, SECURITY DEFINER ownership, RLS policy/column posture, and the
  full `clinic_agent` effective callable surface.
* Re-discover the combined permission census, SQL readers, authority signals,
  native probes, and clock catalogue/opaque pins. Text-merged inventories and
  the resolver-list union are candidates, not integrated parity proof.
* Execute fresh/populated migration upgrades and prove preservation of all
  principal grant versions, transcript state, retention ordering, and the
  reversible teleconsult mode test under the final migration graph.
* Verify the composed truncate guards, outbox transcription/purge interaction,
  recovery rehearsal, static/template translation behavior, full pytest and
  coverage, browser suites, images, and canonical CI on the final t42-inclusive
  source.
* No source-only result opens any real provider, live-data, device, legal,
  clinical-validation, or other external gate.
