Todo 57 implementation commit is `5a52514fe97b750eb8c41f4f7cc902ce1daeaad1`, on source-confirmed todo-56 `914dd542e2207c8c53b277c878b51691c15c2a2f`. Review `gate-review-r2.md` is confirmed-pending-R and includes the inactive-actor delta. Native R and browser qualification remain deferred; independent approval is not claimed.

The preserved tag `wip/cpi-e-t57-pre-inactive-actor` retains `6a3c7666` and `8eea8b34`. Their replayed chain is `23784579` / `7e755511`. Root's pre-existing report append was restored byte-for-byte and remains unstaged.

The unshipped 0012 now requires a matching active user before PSP requester self-access or book authority. Binding and evidence guards inherit that protection; dispatch already delegates to checked book authority. All three signatures have executable native active/inactive/active oracles, with no JUSTIFIED addition.

- Pre-fix native proof: 3 failures (access, binding, evidence), 2 passes (both dispatch flags), expected exit 1.
- Corrected native phase probes: 5 passed.
- All five sibling identity SQL registry modules: 99 passed.
- lane serial scope = 18 required selectors / 109 tests; full ci-serial manifest deferred to native R
- Affected PSP regressions: 57 passed. Current green runs have no errors, failures or skips.
- Full Ruff/format and strict mypy (2398 files), fresh migrate, drift, Django check and role posture passed.
- All 824 inherited Python entries and 465 SQL classification labels remain unchanged. This delta changes only PSP probe declarations. The five discovery-only caller extensions already introduced by the original PSP integration are disclosed in `inactive-actor-r3/base-preservation.json`.
- Owned PG, volume, runtime secrets and temporary fixtures were removed; no private-password leaks were found.

Current receipts: `inactive-actor-r3/native-results.json`, `serial-summary.json`, `base-preservation.json`, `SQL_AUTHORITY_REVIEW.md`, `commands.jsonl` and `cleanup-receipt.json`. The source is committed; the independent canonical clone is historical at 6a3c7666 and must be refreshed by Root's batch.

Current checksums are in `inactive-actor-r3/SHA256SUMS`, checked from this
task-57 evidence root. The original root manifest belongs to the preserved
historical package; Root's externally appended report is excluded from the
new manifest and remains uncommitted.

Browser/full native R remain `deferred-to-host-quiet-batch`; upgrade is excluded by Root. Real providers remain `BLOCKED-ON-EG-1/EG-5`. Earlier broker, Manual-QA, broader test and statement-red receipts are retained as historical evidence and were not rerun for this delta.
