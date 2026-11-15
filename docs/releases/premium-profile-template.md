# Premium release profile record template

**Status: UNAPPROVED — DO NOT USE LIVE DATA**

Copy this template to `docs/releases/<YYYY-MM-DD>-premium-<profile>.md`
when preparing a candidate. Record the exact values below from retained
receipts. An empty or missing value is a release gap, never implied approval.
Do not commit signing keys, credentials, database URLs or clinical content.

| Binding | Required evidence |
| --- | --- |
| Profile | One exact name from `ops/release/profiles.py` |
| Release ID | The `CLINIC_RELEASE_ID` used by readiness and activation |
| Candidate commit | Full 40-character commit from the clean build receipt |
| Candidate tree | Full 40-character tree from that same receipt |
| Candidate hosted run ID | The successful hosted run for that exact commit/tree (L2); include its retained report |
| Evidence root manifest | The digest retained by `ops.release.activation` |
| Readiness report | `check --mode <mode> --profile <profile>` command, exit and retained JSON |
| Owner trust anchor | Public-key identity installed outside the evidence bundle; never the private key |
| Owner decision | Separately signed profile records, with their issue and review dates |
| Disabled capabilities | Browser suite, engine, viewport, report, screenshots and visible `Indisponível` assertions |
| Security and recovery | Accepted assessment and provider PITR/encrypted recovery receipts; scope and synthetic logical drills remain L1 preparation |
| Source currency | Every dependent RV row, retrieval date, digest and next review |
| Rollback | Activation `disable` receipt; preserve records, database and objects |

Run and retain:

```sh
uv run --frozen --no-sync --no-env-file python -m ops.release.readiness check \
  --mode synthetic --profile synthetic-demo
uv run --frozen --no-sync --no-env-file python -m ops.release.readiness check \
  --mode live --profile core-live
```

The first command can pass using labelled fixtures. The second must refuse
until the core owner/provider/security/recovery/source records exist.
Activation is performed only by the owner through `ops.release.activation`;
this document does not set `CLINIC_DATA_MODE=live`.

The baseline hosted run `36045032066` belongs to
`b39ae436590c8a9c00ad8fc93d619875d1822827`. It is historical context and
cannot be entered as a newer candidate's hosted run.

Reference local evidence by path and level:
`.omo/evidence/clinic-ops-premium-intelligence/task-71/` (performance),
`task-72/` (synthetic recovery), `task-73/` (security scope), `task-74/`
(economics), `task-75/` (journeys), `task-76/` (study protocols) and
`task-77/r1/` (profile checks). Verify each against the chosen candidate;
neither these paths nor the template assert that those gates passed.

Record any refusal, excluded capability, missing hosted run or external
decision explicitly. Do not tick LIVE-DATA-GATE.md or mark any EG gate
satisfied from this template or an agent's L1 evidence.
