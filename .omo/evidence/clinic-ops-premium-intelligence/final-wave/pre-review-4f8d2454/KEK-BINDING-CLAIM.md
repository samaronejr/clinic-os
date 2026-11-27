# KEK binding — defensive verification

## R4f — implicit owner authority and pinned process-group cleanup

Product `6820766c6d02ae9baaca01e09facd0a14167ce0c` (sole parent `a3013b5b2fe360e1502f459f325ceb6be0de4469`, tree `3ff4820011cd9ff91e78401f7e3bea6c7e2356a2`) is bound to `refs/heads/ulw/cpi-h-kek-binding-r4f` and its evidence-only child to `refs/heads/ulw/cpi-h-kek-binding-evidence-r4f`. The round-five REQUEST_CHANGES on r4e is preserved; that old certificate does not certify r4f. This is local synthetic implementation evidence, not independent review, hosted CI or deployment clearance.

**Ownership.** The inspector walks the target-local direct and recursive runtime role family before consulting ordinary privileges. Runtime-reachable database and schema owners remain forbidden. Owned tables, partitions, views, foreign tables, sequences, types and large objects have no exception. A routine owner is in scope in every namespace even when EXECUTE was revoked from both owner and PUBLIC. Any exception must match the exact signature, identity arguments, owner and definition, and explicitly authorize the owner's and runtime root's delegation; listing an ordinary executor is insufficient. No deployed owner exceptions or ACLs were added. All 608 deployed signature/ACL/lineage records match a fresh replay of 259 migrations; none permits delegation. The migration-only path is preserved.

**Process lifetime.** The private Make worker polls a pidfd without consuming the wait status. It signals while the direct leader remains unreaped, then waits for the leader and adopted children using only direct child identities; it issues no numeric group signal, including signal 0, after release. The deterministic Event-synchronized kernel model reuses a numeric PGID for a foreign job and shows zero signals to it; no real foreign PID is targeted. An independent real private group covers a successful leader leaving an owned child. The real timed-out launcher and child were reaped. Observer teardown has a separate budget; a successful probe at 15 seconds is still refused.

**Discriminating controls.** With exact parent verifier/worker blobs, eight of nine comparison cases hit the intended new assertions: seven catalog false greens and one stale attempted signal in the model. The ninth case, a runtime-owned schema after ordinary ACL revocation, was already refused by r4e; calling it a new red would be false. Removing that schema arm on r4f fails the test. On the successor, all 12 added tests passed. Of 48 omission probes, all **47 non-equivalent** omissions hit their intended assertion (37 prior, nine new ownership controls and the PGID model); removal of the explicit `rolsuper` arm remains equivalent because of effective superuser routine authority. The table omission includes its linked row and array types. Both independent committed recorder mutants failed at the original exact equality. No existing assertion or privilege boundary was weakened. An initial development probe failed before its eight catalog assertions because a formatted psycopg `Composed` was formatted again; the corrected and subsequently exact parent evidence is the one counted here.

**Exact gates.** Actual system CPython 3.12.3 (`/usr/bin/python3.12` via `.venv/bin/python`) opened and closed `anon_inode:[pidfd]` in the certifying pytest process. SC-5 `cpi-kek-r4f-sc5-6820766c.service`, invocation `ad6ec57c75dc4818878b1a4ad780a623`, Nice=10, exit 0: **16,677/16,677** independent public IDs passed once, with exactly one passing setup/call/teardown; 16,665 prior IDs (including all 11 pidfd-sensitive IDs) plus 12. Zero missing, extra, skipped, duplicate or nonpassing IDs; coverage 93.59%. Five-field manifest SHA-256 `60742f26505eda9d85c3b0e535cf7c1b41c309e5a99386a0ea7f6e6a004b361e`; native sanitized JUnit SHA-256 `c8c858436b70cd142756c89a2cb31dbf2be29598f12316fb2ddd0fba113b54e6`. Ruff, format, strict mypy (2,958 files), Django checks, migration drift, both migrations/databases, real posture, all seven baseline/refusal/recovery sequences, 86 focused tests, and the independent 3,308 isolation cases (including six restore guards) passed. The main test DB's 126 guards were enabled before/after, none repaired. No auxiliary run supplies SC-5 rows.

**Same lease.** After the real populated migration, leaf reinstallation and clone teardown: 121.502 s idle, warm 0.344 s, distinct fresh 0.345 s, both strictly below 15 s. Injected launcher wait 15.067 s; zero leftover client backends and owned group reaped. The trace measures this invocation, not the cause of a historical stall.

`SC5-6820766c/replay_r4f.py` validates the bundle. The private main PG16.14 cluster alone used fsync=off (started 2026-11-26T19:48:48.720571111Z); no real clinical data or release credentials were exported. Publication is an atomic non-forced push, a cold fetch and comparison of every file, then shutdown of only the labelled lane resources. `SC5-6820766c-publication.json` is the local delivery receipt written after cleanup; the final chain exit is checked separately.

## Historical runs — not r4f evidence

## R4e — catalog authority, delegation, overloads and traced same-lease handoff

Product `a3013b5b2fe360e1502f459f325ceb6be0de4469` (tree `f976bc8161a7ff524f5c76ddc2db6074964bf272`, sole parent `31938a30ab9bfc8a5cb9da9568d834719fbab975`) is on `ulw/cpi-h-kek-binding-r4e`. Its evidence-only child is on `ulw/cpi-h-kek-binding-evidence-r4e`. The round-four review returned REQUEST_CHANGES on the r4d parent; that parent's passing suite does not certify this revision. Prior product and evidence refs were left unchanged. This section is the implementing lane's evidence, not independent code-review or deployment clearance.

**Owners and ACLs.** The gate authenticates the configured staff login, takes its trusted primary snapshot and closes that transaction and connection before inspecting all `pg_database` rows as the independent posture login. Database name, OID and endpoint pinning remain. Each target contributes its own `datdba` → `pg_database_owner` edge to each login's complete inherited, SET-only and ADMIN-only closure. A runtime root reaching the owner role is refused with **no additional grant**. A runtime-reachable role owning a namespace is also forbidden, independently of membership in the database-owner role. The migration owner's intentional path is not a runtime exemption. Both the prior delegated-routine denial and the new bare-owner denial are exercised. Monitoring components are independently forbidden even when their mutable default PG16 overlaps are absent.

Restricted native and extension functions still follow current and initial PUBLIC EXECUTE ACLs. All non-extension application routines are protected even with default PUBLIC EXECUTE or in a database whose application schema hosts no extension. An exception is keyed by its exact OID-derived regprocedure **and** `pg_get_function_identity_arguments`, owner and definition hash. Grantee names and EXECUTE **WITH GRANT OPTION** are different sets; none of the 608 reviewed entries permits delegation. A new overload, ambiguous or malformed policy row, unauthorized grantee, or unauthorized delegation fails closed. Native and extension-member exceptions: zero.

**Origins independently witnessed.** A new cluster replayed 259 committed migrations on this SHA. All 608 final identities, parameter declarations, owners, definition hashes, grantees, grant options, creation, installed-name and last-definition migration citations matched the committed policy. `intake_duplicate_transition_guard()` originates in `intake/0017_patient_aliases`, not `0016`. The exact `tenant_decrypt(text,text,bytea)` reason now distinguishes the restore-only admin grant from staff and definer access. The clean approvals were not widened or learned from the deployment. The provenance scripts and their full per-routine result are in `SC5-a3013b5b`.

**Controls.** All 37 non-equivalent omissions were caught out of 38: the previous native/extension inventory, closed/no-CONNECT database and membership checks, plus bare runtime and independent machine ownership, namespace ownership, delegation, collapsing overloads by name, ignoring a default-PUBLIC overload and requiring an extension for the app schema. The isolated component probes now catch `pg_read_all_stats` and its monitoring peers even without the stock overlapping catalog grants. The explicit `rolsuper` omission is reported separately as equivalent on stock PG16: a superuser still has effective EXECUTE on restricted routines. No branch-specific superuser test was added. The seven original full-posture verdict sequences were retained and strengthened where bare ownership now forbids an additional intermediate state. Both independently committed recorder mutations still failed at the original final exact equality; explicit leaf reinstallation and earlier checks remain.

**Timed diagnostic.** After actually running the prior committed mode replay and closing/dropping its clone, the same module-scoped lease was idle for 204.156 s during an independent fresh control. The real Make/uv/verifier took 0.665 s on the handed-off lease and 1.718 s on the fresh lease; the deadline stayed **15 s per invocation**. Trace verifies same/distinct identity, zero leftover client backends, closed primary before the peer scan, per-target phases, and sampled process/backend states, wait events, blocked counts and transaction ages. The deliberately stalled test-only launcher was observed at the launcher stage and timed out in 15.000 s; its process group and descendant were reaped. No query text, credentials or raw arguments were recorded. This diagnoses the deliberately injected wait; it does not retrospectively identify the unmeasured cause of the older `5c543f47` wall timeout.

**Rejected attempt on the same SHA.** Invocation `6e314fbb752c431f8312eca63f688966` and its release chain exited 1; nothing was pushed. It had 16,654 passing IDs, 10 failed calls and two phase errors across 11 distinct affected IDs. The managed CPython 3.12.12 build lacked native `os.pidfd_open`; supervisor cleanup failed and unreaped-child warnings affected subsequent phases. A fresh locked venv on system CPython 3.12.3 opened a real kernel pidfd; all 11 affected IDs then passed separately. The rejected JUnit, phase reports, systemd result, attribution, and separate preflight are retained. No source change, shim, skip, assertion change, or rows from either run were used to obtain the certificate.

**Interpreter used for certification.** SC-5 ran via the checkout’s `.venv/bin/python`, backed by **`/usr/bin/python3.12` (CPython 3.12.3)**. At startup the actual pytest process verified that both paths resolve to the same executable and that `os.pidfd_open` comes from `posix`, opened its own PID with `os.pidfd_open(os.getpid())`, observed `anon_inode:[pidfd]` in `/proc/self/fd`, and closed the descriptor. `SC5-a3013b5b/interpreter.json` binds that observation to `0deb533bd038447f9dd3494682bfee3c`. `SC5-a3013b5b/pidfd-native-full.json` reconciles all 11 unchanged affected IDs to the certifying full run’s native JUnit and passing setup/call/teardown; the separate 11-case preflight is not counted.

**Second rejected attempt.** Native invocation `71d05494cece40ecb1c455a426bf98ba` passed all 11 earlier failing IDs, but exited 1: 16,664 passed and one failed, no errors or skips. The unchanged cross-clinic identity race did not observe both OTP commits within ten seconds (call 10.065 s); it never reached the assertion after releasing the canonical-email gate. The red run did not record worker stacks or cgroup counters, so it cannot identify a particular scheduler or database blocker. On the same SHA under covered execution, the quarter-core diagnostic **failed** in 10.773 s; under the shared DB lane lock with no CPU quota the ordinary control passed in 2.304 s, and the full-run case passed in 2.018 s. Same product, test, assertions, deadlines and native interpreter; no test reruns within any SC-5. Both rejected attempts are archived separately and supply zero rows.

**One clean SC-5.** `cpi-kek-r4e-full-serial-a3013b5b.service`, invocation `0deb533bd038447f9dd3494682bfee3c`, Nice=10, exit 0: **16,665/16,665** independently collected public IDs passed once with one passing setup, call and teardown; zero failures, errors, skips, missing, extra or duplicates. The certifying execution had fresh coverage and exactly the same independently collected ID set as both rejected runs. Includes all 16,651 old IDs and all 14 additions; coverage 93.59%. Native `SC5-a3013b5b/junit.xml` SHA-256 `f4bb4064e3e5d27c7c157fca1b2bc8b45b8965b6cbf912b742c9f48c87d1733d`; rebuilt five-field `SC5-a3013b5b/outcomes.jsonl` SHA-256 `ac7d3b53d65a16d8fc5aa0318b82a575f0c292231036c608a43b498fefe85f83`. Static checks (Ruff lint/format, strict mypy on 2,956 files), bootstrap/migration, Django checks/drift, real posture, 74 focused tests, all mutation validators and the separate 3,308-case isolation suite (including six restore guards) passed. None of these independent checks supplies release rows.

**Publication and scope.** PG16.14, Redis 7.4.11, real broker/PDF prerequisites, umask 022, all 126 guards verified. Only the disposable main PG instance used fsync=off (started 2026-11-26T04:58:45.448959941Z). The origin and handoff leases were independently provisioned and disposed. The publisher must push both fresh refs atomically without force, cold-fetch them and compare the claim and all artifacts byte-for-byte **before** deleting the lane’s resources. The local delivery receipt `SC5-a3013b5b-publication.json`, written only after those actions, records the evidence commit, the cold readback time, the main cluster’s removal time, port and resource cleanup, and the final claim match. The release-chain systemd exit is independently checked after the publisher returns. No hosted CI, browser, live deployment or live-data safety claim.

## Historical runs — not r4e evidence

## R4d — current candidate; all earlier r4 candidates rejected

Product `31938a30ab9bfc8a5cb9da9568d834719fbab975` (tree `f5ab0e74ed1806794abe846dd1ef18c83579724b`, **sole parent** `de424fa482adc1346392049fbf2d556cb05c957d`) is published on `ulw/cpi-h-kek-binding-r4d`, with separate evidence-only child on `ulw/cpi-h-kek-binding-evidence-r4d`. The older `ulw/cpi-h-kek-binding` ref had already been published on a rejected line before r4d was requested. It is historical and is **not** the current candidate; r4d neither pushes bb64a1de nor rewrites that ref. This section supersedes all historical approval or release language below. No independent review or deployment is implied.

**Seven real positive/refusal/recovery legs.** A cluster-wide gate cannot be isolated by creating just one database in a shared cluster. A separate PostgreSQL lease per consuming test module creates and migrates both fixture databases and uses its real owner/app/superuser/inspector identities. The parent scheduling template remains sealed and was not skipped or temporarily opened. The diagnostic verified four distinct module cluster identities, without recording them. All seven named tests observed a clean baseline, a refusal under their targeted mutation and a successful recovery; 60/60 focused positives. `focused.json` and its independent native JUnit prove the actual booleans, including the Make URL probe's wrong credential and recovery. The fixture was a different cluster from the parent with the sealed template. Actual operator deployment posture was independently run on the main release cluster. All r4 product/posture blobs, including the 608 explicitly reasoned, fingerprinted app-schema exceptions, are unchanged from de424fa4.

**Strict committed migration rehearsals.** Both tests start with real RBAC, provider, populated graph, session scopes and ownership, on a separate fully migrated, unsealed-template clone per test. All historical operations commit. The original graph rollback refusal, explicit reinstall of the graph's leaves, reverse/forward operations, catalog and history comparisons, final exact applied-migration set, and subsequent runtime/audit assertions retain their execution order; there is no outer rollback hiding recorder writes. An additional exact set equality follows the leaf reinstall; capture disclosure is replayed and included in the catalog hash. In a distinct diagnostic, a real extra migration-recorder row was independently visible after commit and caused the original final equality in **both** tests to fail. Neither failed on setup or teardown. The two clock helpers restore and verify the exact installed definitions without changing their frozen instant or domain assertions.

**Assertion review.** `assertion-review.json` and its reproducible Git-object comparator classify the diffs against de424fa4 and 4f8d2454. The former changes only nine test files and adds 13 checks (206 preserved within changed files). Against the wave: 76 classified additions/replacements, 241 preserved within changed test files. No prior requirement is removed, observed only after rollback, or replaced by a looser set, match, or allowlist. The mandated binding and logging fields are exact requirements in addition to the historical contract. Scope and limitations are detailed in the bundle.

**Rejected history (not merged runs).** `de424fa482adc1346392049fbf2d556cb05c957d` and `661483f07919db74a27cec36875a1c23e8bebc8e` each collected 16,651 and had the same 7 failing clean-baseline assertions: scheduling's sealed template outlived its tests. The fixture environment—not the product scanner—was wrong. Independent clock/capture test-definition drift was also found on de424fa4. `661483f0` attempted rollback cleanup but failed the same baseline and did not preserve all migration-rehearsal coverage. `bb64a1de35504b5582b8d3adf0183f220f472ff7` passed its own 16,651/16,651, but was explicitly **rejected**: the recorder comparison followed forced rollback and original explicit leaf reinstallation had been removed. That run cannot certify r4d. Rejected per-case manifests and failure attribution remain separately identified in the earlier evidence.

**Additional rejected, unpublished r4d attempt.** `5c543f47b01d7c44b08b1c531170800bdd01aee9`, invocation `7a8c8542afe7422498a155a9c3318a19`, had 16,650 passing IDs and one `subprocess.TimeoutExpired` in `test_db_posture_authenticates_the_supplied_app_database_url`; no errors, skips, missing or duplicate IDs. The 15-second Make subprocess deadline expired during the call, not during fixture setup or teardown. The aggregate intervening test duration since the session lease's prior consuming module was about 36 minutes. The trace does not pinpoint the operating-system delay. Its observed manifest `SC5-5c543f47-rejected/outcomes.jsonl` is SHA-256 `b24787ce5adf7c49aaf26af3415c1816c303f14d201eca4f709fbf02b1792857`. The new direct child of de424fa4 does not use that run's results. Relative to that r4d attempt, only the lease's scope and explanatory docstring change: each consuming module now gets its own freshly migrated cluster. The Make invocation and its 15-second deadline are byte-for-byte unchanged. No retry, time allowance, skipped assertion, warmed-verdict substitution, or production exception is used.

**One clean SC-5.** `cpi-kek-r4d-full-31938a30.service`, invocation `a9f65c89de80459e80ce793491f007bb`, Nice=10, exit 0. All **16,651/16,651** independently collected IDs had exactly one passing setup, call, teardown and native JUnit case; zero failures, errors, skips, missing, extra or duplicate IDs. Coverage **93.59%**. The five-field PHI-free manifest is `SC5-31938a30/outcomes.jsonl`, SHA-256 `94bdbcb292bdb23207b0445abe5ed240436914d6a96cfc82860dd2a25380bee5`; source JUnit SHA-256 `540b32ac2f9ae1ee11854c55453949dc3b17c5f41b321f4fe0fb80ef65a5c0cb`. Replay and checksums compare the manifest byte-for-byte. Static gates, bootstrap, owner migration, Django checks/drift, live deployment-posture entry point, Ruff lint/format, strict mypy (2,951 files), all four r4 requested mutation controls, six restore guards and 3,308/3,308 independent harness tests passed. The two independently redundant r4 controls remain: the explicit superuser check, and the pg_read_all_stats role arm (effective EXECUTE on restricted routines also denies them). Evidence covers synthetic non-browser fixtures only.

**Cleanup.** No disabled guards (126/126) or other client backends at shutdown. The main private PG16.14 volume alone used fsync=off (2026-11-25T18:28:16.254143495Z to removal by 2026-11-26T04:16:08+00:00); the test-created PostgreSQL leases used their ordinary settings and were released. Redis 7.4.11 was required on PATH, with the real broker/PDF gates and umask 022. The publisher checked container/volume identities and mounts before removal, rebound and released its private ports, pushed only the two new refs without force, fetched both into an independent object store, checked the product tree plus evidence/claim bytes, and verified scratch, venv and temporary worktree removal.

### R4 post-publication verification — November 26, 2026 (UTC)

**Publication completed, not running.** SC-5 invocation `a9f65c89de80459e80ce793491f007bb` exited 0 on `31938a30ab9bfc8a5cb9da9568d834719fbab975`: 16,651 collected, 16,651 passed exactly once, zero failures, errors, skips, missing, extra or duplicate IDs; 93.59% coverage. Native JUnit, independent collection and setup/call/teardown replay give manifest SHA-256 `94bdbcb292bdb23207b0445abe5ed240436914d6a96cfc82860dd2a25380bee5`. The publisher and the release chain completed at 04:19:13 UTC. Read-back from `origin` returned that product SHA and evidence-only child `4157ef87c725b0332ad62f0c1ce806ffc308bc97`. This local post-publication entry supplements that immutable evidence; neither ref was pushed again. Static, focused-positive, recorder-validation and isolation gates exited 0. Diagnostic recorder pytest exited 1 for the two intended assertion failures; catalog-mutation pytest exited 1 for 29 intended failures and two verified redundant denials. Neither diagnostic contributed an SC-5 row.

**What the timeout trace establishes.** The red case was `tests/infra/test_db_posture_url.py::test_db_posture_authenticates_the_supplied_app_database_url`, not a failed permission assertion. The values below are elapsed seconds for that exact node in the two full suites' `phases.json.gz`. “Call” is the whole pytest test body, **not** a per-query or per-subprocess measurement.

| Product | PostgreSQL fixture lifetime | Pytest setup | Pytest call | Teardown |
| --- | --- | ---: | ---: | ---: |
| `5c543f47` | Session: reused the already-migrated cluster | 0.006 | 15.076, `subprocess.TimeoutExpired` | 0.001 |
| `31938a30` | Module: newly provisioned and migrated for the URL-test module | 145.000 | 1.972, pass including valid / invalid-password / valid | 0.001 |

In the old run there were 4,849 intervening tests, totalling about 2,176.7 seconds of measured phases, between the last rehearsal-module use of the private lease and this URL test. The next five invalid-URL cases took 0.225–0.366 seconds each; the later genuine transitive-parameter posture test passed on the same lease in 0.311 seconds. The separate positive diagnostic on that SHA also observed success/refusal/recovery for the URL case. This is evidence against a permanently stuck cluster. `systemd` recorded swap peaks of 1.7G (old) and 574.9M (new) for the respective **whole full-suite units**, not the Make child during the timeout. A transient stall or cold resource access under load is plausible, not proved. The old trace contains neither per-invocation timestamps for the three Make calls nor a contemporaneous backend connection-count, lock/wait-event or child-I/O timeline. It cannot identify the blocked operation or establish “load rather than a leak” as the underlying cause.

**Strictness and scope.** `git diff 5c543f47..31938a30` changes exactly `tests/isolated_pg_fixtures.py`: `migrated_pg_cluster` goes from session to module scope and gains a docstring. Provisioning happens in setup in **both** versions. `_run_posture`, its 15-second per-subprocess deadline and every assertion are unchanged. The recorder locations cited for the older implementation have shifted: on this SHA the event test reinstalls the leaves at line 72 and checks the live recorder at lines 73 and 115; the mode test does so at lines 110, 111 and 145. Both entire test files have identical Git blobs across this diff. A separately connected observer saw each injected recorder row committed, and both final live equalities failed before clone disposal. All seven posture tests retained baseline/refusal/recovery; the secondary-grant test refused in three successive grant/CONNECT states before recovery. All four targeted catalog/database omissions failed their intended assertions. All 3,308 independent isolation cases passed, including six restore guards.

**Cleanup recheck.** No lane units or timers, processes, containers, volumes, labeled networks, module leases, temporary worktrees or scratch roots remained. No PostgreSQL cluster was restarted to infer historical wait states. No product source, independent review, or sealed run artifact was changed by this verification.

## Historical runs — not r4d evidence

## Round 4 — derived routine authority and complete database inspection

Product `bb64a1de35504b5582b8d3adf0183f220f472ff7` (tree `4a07860158334b82ba7dc7ccfeff9828092050d9`, sole parent `661483f07919db74a27cec36875a1c23e8bebc8e`) is on `ulw/cpi-h-kek-binding`. `ulw/cpi-h-kek-binding-evidence-r4` is a separate evidence-only child. This supersedes the round-three security claim, not its historical results.

**Catalog rule.** Every `pg_catalog` or extension-schema routine with a non-NULL ACL withholding PUBLIC EXECUTE is restricted. Initial ACLs in `pg_init_privs` also catch a newly public native/extension grant. No native routine or extension member is allowlisted. Three shipped extensions share the application schema. Its 608 exact, migration-provenanced private overloads pin the owner, canonical PG16 definition SHA-256, grantees and audience; 313 permit the staff login and 21 permit the canonical machine role (overlap is possible). The remaining migration/definer-only contracts do not give the app EXECUTE. Membership exposing such a helper, a different body or overload, or a widened ACL refuses. The gate does not learn permissions from the inspected installation. The cluster checks now also reject replication and derive the monitoring family from pg_monitor membership.

**Database boundary.** The app first authenticates to its configured database. The catalog inventory includes every pg_database row regardless of datallowconn, CONNECT, owner or template status. A distinct NOSUPERUSER/NOBYPASSRLS `clinic_posture` login checks each on the selected endpoint with the caller's transport/TLS options; an identity/name/OID mismatch or unavailable catalog fails closed. Each database contributes its own implicit datdba → pg_database_owner membership in the complete per-login closure. PG16 cannot connect even a superuser to closed template0. Administrative bootstrap explicitly confines it to its superuser owner and the inspector before opening it; the gate never opens it. On managed hosts which do not permit inspection, no complete attestation is claimed. Use a quiescent direct cluster endpoint. No live credential, query contents, patient data or privileged operation appears in these proofs.

**Proofs.** All four requested omission controls were killed: native memory routine, unknown randomly named restricted routine in an extension schema, hidden machine-only database and reachable local database owner. Both the real grant and its revoke, and the full authority path, were exercised; no native privileged routine was invoked. Machine-only coverage authenticates the alternate suffixed login while clinic_app cannot directly CONNECT. The owner witness shows direct app EXECUTE remains false, and the reached owner has effective EXECUTE. Complete diagnostic: 29/31 targeted mutants killed, two redundant omissions still correctly denied on PG16 (`rolsuper`, and `pg_read_all_stats`, which also has EXECUTE on `pg_get_backend_memory_contexts()` and `pg_get_shmem_allocations()`). An earlier diagnostic on the rejected predecessor expected only one equivalent omission and exited 1. Its observations are retained and distinctly SHA-bound; the new release has its own correctly attributed diagnostic. All seven prior r3 omissions had been caught on their previous SHA; this is a separate inventory on a different mechanism.

**First rejected predecessor.** The first audit implementation, `de424fa482adc1346392049fbf2d556cb05c957d`, was not published as the code head. Its single SC-5 invocation `6d2e94a370494b92bdd6c6f516304113` returned 7 failures, 16,644 passes and no skips/errors. PostgreSQL recorded eight connection refusals to a sealed scheduling template during the failing window. `SC5-de424fa4-rejected/run.json` records each of the seven exact test IDs, source lines, failed assertions and causes. The post-run verifier independently exposed three stored-definition leaks in `test_clinic`: two time fixtures restored a reformatted SQL body, and two migration exercises recreated the pre-disclosure capture routine without the later encounter-specific consent check. `git diff de424fa4..661483f0` changes only five files under `tests/` (49 insertions, 87 deletions), not only fixture declarations: it updates fixture consumers, migration test bodies, and catalog/guard/history assertions. It restores the exact clock definitions and isolates historical DDL in rollback-only transactions while replaying the dependent reversible disclosure migration. No product, posture, gate or allowlist code changed. No predecessor result was used for the certified manifest.

`SC5-de424fa4-rejected/outcomes.jsonl` (SHA-256 `f7ec7b6af610a91b60b13f8140d2af8b5a98430ec03b16b91d76799789e948b7`) is the predecessor's observed, **failed** manifest. Its 16,651 IDs each appear once: zero missing, extra or duplicate. Its replay checks each JUnit verdict and duration against the independent collection and three pytest phases. The full process, witness and guarded publisher each exited 1; the separate 3,308-case harness and the corrected static/posture/mutation wrapper exited 0. No all-pass manifest exists for that SHA.

**Second rejected predecessor.** `661483f07919db74a27cec36875a1c23e8bebc8e`, invocation `36aecde2c8084367a6b47f4221757489`, separately passed 16,644 and failed seven, zero skips/errors. No push followed. Its stored definitions were correct afterwards. PG recorded eight refusals to a sealed scheduling template during the failure window; the deployment verifier was green after session teardown. `SC5-661483f0-rejected/outcomes.jsonl` SHA-256 `12865ffbd3cc711fb350413015ea44e75703fcde0f7ba9201f60447fa1e032f0` retains those actual outcomes. Neither rejected ledger supplies a release row.

**Scheduling isolation.** A three-case regression ran the original template-seal and worker-clone tests, followed by the actual deployment-posture command. Before the correction it passed both scheduling controls and failed only posture; after the correction all three passed. The template still denies every connection while scheduling uses it. Its package-scoped factory now drops the template after the last clone and Django trigger teardown, before a different package runs. Interrupted sessions retain fallback cleanup. Neither product code nor its reviewed allowlist changed in either fixture fix.

**One clean SC-5 run.** `cpi-kek-r4-full-bb64a1de.service`, invocation `7472575ca4e3404bb27014085a95729b`, Nice=10, exit 0. All **16,651/16,651** independently collected public IDs (the 16,642 previous IDs and all nine additions) have one passing setup, body, teardown and one native JUnit result. Zero failed, errored, skipped, missing, extra or duplicate. Coverage **93.59%**. Rebuilt directly from `SC5-bb64a1de/junit.xml` (SHA-256 `6113988af317f535d6efe430ba257e0c46e17cfff5372a12e63a99756123902f`); the five-field `SC5-bb64a1de/outcomes.jsonl` SHA-256 is `2609d5587b5f80fb56bd8152720b8b96dd3028b8d04b9f92f321ffea00a0077f`. `verify.py` compares the bytes, set and three phase durations; no summaries, retries, focused or mutation results contribute to the release manifest.

**Other gates and cleanup.** Locked Python 3.12, PG16.14, real Redis 7.4.11, Poppler, real-broker gate and umask 022. Bootstrap/migration, posture, table-trigger guard, Ruff lint/format, strict mypy (2,949 files), Django system checks, migration drift and diff checks passed. 60 focused positives and an independent 3,308/3,308 isolation run, including all six backup/restore guard tests; its collection matches the release subset. The disposable PG instance alone ran fsync=off from 2026-11-25T10:21:05.775301373Z until cleanup by 2026-11-25T18:19:30+00:00. 126/126 guards enabled before/after; no other client backends at shutdown. Both generations of identity-checked containers and volumes were removed and loopback ports released. Publication is non-forced, both refs are read back, and only the lane's worktrees and private scratch are removed. No hosted CI, browser, live restore or deployment claim.
## Historical round-three and earlier evidence

## Round 3 — cluster authority and database-local function ACLs

Successor `b7a4bcb62f84965d84616ab22bf7dd9695bd1df5` (tree `1443643b0de5c84e1d0959609bd4fc2938f13977`, sole parent `6b84a4faf05183d3ac622d212c51bbdeb58987f6`) closes the r2 review’s
remaining policy gaps. The tested product and the independently replayable
evidence are separate Git refs: `ulw/cpi-h-kek-binding` and
`ulw/cpi-h-kek-binding-evidence-r3`. The sections for prior commits below are historical.

**What changed.** The recursive membership query keeps the originating login
along every edge. It rejects reachable CREATEROLE and BYPASSRLS (the only
exception is `clinic_owner` reaching the NOLOGIN `clinic_resolver`, never
`clinic_app` reaching that same role), superuser, `pg_signal_backend`,
`pg_read_all_stats`, the existing file/program/settings/monitor roles, and
all delegated parameter SET/ALTER SYSTEM. ADMIN-only, SET-only and inherited
paths remain in the closure. Nothing signals a backend to prove this.

Function privileges are tested by function OID, across all overloads of
`lo_import`, `lo_export`, `pg_read_file`, `pg_read_binary_file`, `pg_ls_dir`,
`pg_ls_logdir` and `pg_stat_file`. The actual runtime login enumerates
`pg_database` with `datallowconn` and effective `has_database_privilege(...,
'CONNECT')`. On this isolated cluster that includes the app DB, test DB,
`postgres` and `template1`; `template0` does not allow connections. The
verifier inspects each target on the selected host/address/port with the same
credentials and TLS settings. It verifies the actual login, current role,
database name and OID, pins catalog resolution, and refuses an inaccessible
target. The check assumes quiescent role/database configuration, not an
atomic snapshot spanning databases. No runtime crypto, send-outcome or
frozen-plan code changed.

**Regression and mutation evidence.** 43 focused checks passed, including
the old controls and ten new r3 cases. An isolated, PUBLIC-connectable extra
database with the only hazardous function grant caused refusal; removing
CONNECT made it out of scope, granting CONNECT restored the refusal, and
revoking the routine grant restored success. A database with CONNECT but no
available connections also caused refusal. Individual rollbacks proved the
new roles, attributes and function names behind a multi-hop ADMIN-only edge.
No elevated operation, file read/write, backend signal, or key recovery was
performed. `SC5-b7a4bcb6/mutations.json`, `mutation.xml` and `mutation_probe.py`
retain the exact per-control outcomes, JUnit and mutation machinery.
All seven r3 privilege omissions and both secondary-database omissions
failed at their intended assertions; the app/resolver-root and binding
controls failed as intended too. In all, 21/22 mutants were killed.
The **extra** mutation removing only the pre-existing explicit `rolsuper`
comparison did not open the gate: superusers have effective EXECUTE on the
forbidden routines too. Its test correctly continued to reject the role.
The initial diagnostic wrapper expected 22 failures and exited 1; the JUnit
and attribution have been retained intact, not reclassified as 22 kills.

**One SC-5 execution.** Unit `cpi-kek-r3-full-b7a4bcb6.service`, invocation `d87276f81d3f4fda84543daf033ae810`, Nice=10,
`ExecMainStatus=0`, clean exact commit before/after. The independent
collector matched all 16,632 base public IDs and ten new ones. All
**16,642/16,642** passed setup, body and teardown exactly once; zero failed,
errored, skipped, missing, duplicate or extra. Coverage **93.59%**. Retained
native JUnit `SC5-b7a4bcb6/junit.xml` SHA-256 `39950e616d2e275078f8b50512f7f70483107baca875ed829e6f730b224b8735`; five-field PHI-free
manifest `SC5-b7a4bcb6/outcomes.jsonl` SHA-256 `06c0dac6017fd13792d6a68ae4eccdd485f785a0ad4b8daeeb81190aa20b21fe`. `verify.py` regenerates
its exact bytes from the JUnit and checks the independent collection and
durations against all three phases. `SHA256SUMS` seals the companion files.

**Other gates.** Locked Python 3.12/PG16.14, actual Redis 7.4.11 executable,
Poppler and real-broker gates required, umask 022 under private roots.
On this SHA: fresh bootstrap/migration, Ruff, format, strict mypy (2,949
files), Django checks, migration drift, `db-posture` and `git diff --check`
passed. The standalone, fake-only ops/testing harness passed **3,308/3,308**;
its identity set equals the full run’s isolation subset and includes the six
backup/restore guards. These are synthetic, non-browser tests, not a full
live restore rehearsal, hosted CI, or independent review approval.

**Cleanup.** All 126 teardown guards enabled and zero other client backends
at shutdown. Under sustained host I/O pressure (~18% full over five minutes)
only the disposable PG container had `fsync=off`, from its start at
2026-11-25T01:11:16.018399075Z until its removal by 2026-11-25T05:25:19+00:00. The two
identity/label/mount-checked containers and both named volumes were removed;
ephemeral ports 33359 and 33360 were rebound and released. The workflow
publishes without force, reads both remote refs back, removes only the
disposable worktrees, virtualenv and scratch, and checks the refs after cleanup.

## Historical `6b84a4fa` and `76376101` records

## Successor: P1 role posture and retained SC-5

The product branch now points to `6b84a4faf05183d3ac622d212c51bbdeb58987f6` (tree `491dd5a2d4955c18d3242cd3f00a86bb0747e6f0`), one commit after
the original binding fix. The independent code review at `76376101` found
that checking login OIDs could miss a privileged, non-inherited role reachable
via `SET ROLE`. The previous full-run totals also lacked retained per-case
primary evidence. The historical claim below is unchanged except for this
superseding status; it is **not** the evidence for the successor.

**Production fix.** `ops/db/posture.py` now walks the complete membership
graph of every covered login. The graph is deduplicated with recursive UNION
and is used for both role settings and privilege checks in one repeatable-read,
read-only catalog snapshot. It rejects reachable superusers; delegated SET
or ALTER SYSTEM on any parameter ACL (including PUBLIC); monitoring, server
file and server program roles; and effective EXECUTE on every built-in
overload of pg_read_file, pg_read_binary_file, pg_ls_logdir and lo_import.
This is conservative even for a membership edge currently not usable. No
runtime query, encrypted field, tenant GUC or outbound workflow was changed.

**Negative evidence.** A real two-hop SET-only grant made the deploy verifier
refuse while the direct `clinic_app` parameter-privilege check remained false;
revoking the membership restored success. Thirteen additional transaction-
rolled-back grant cases cover the protected powers and inherited-after-switch
chains, without executing privileged server operations. Removing the recursive
edge caused the end-to-end posture regression to fail. Disabling binding only
on the tagged producer separately failed the public-canary assertion before
any secret-bearing SQL. The positive focused gate passed 33/33.

**Single-run evidence.** Exact-SHA unit `cpi-kek-sc5-full-6b84a4fa.service`, invocation
`d3092b07d8984d0e84a487a29c19c5af`, exit 0, Nice=10. 16,632 separately collected non-browser
IDs: all 16,618 original IDs and 14 new P1 cases. All 16,632 had one passing
setup, body, teardown and one JUnit case in this invocation: missing, duplicate,
extra, skipped, failed and errored counts all zero. Coverage 93.59%.
The primary JUnit is retained without properties, captured output, exception
text or raw parameter IDs; this is not a reconstruction from counts or retries.
Manifest: `SC5-6b84a4fa/outcomes.jsonl`, SHA-256 `475a62d54cea8370b398a10c5a258d8049f1847a5737fda6b5def7cd8bc22d65`. JUnit:
`SC5-6b84a4fa/junit.xml`, SHA-256 `30039f4077ee38967b5404b84b5f94fae31c5888e247c02ca096ff57e28fd8fa`. `SC5-6b84a4fa/README.md` and the
included reporter, reference encoder and stdlib verifier document exact
identity and duration semantics. `SC5-6b84a4fa/SHA256SUMS` seals all artifacts.

**Other gates.** On this exact code commit: Ruff, formatting and strict mypy
(2,949 files), `manage.py check`, `makemigrations --check --dry-run`, bootstrap,
migration, `db-posture` and `git diff --check` passed. The independent ops/testing
isolation harness passed 3,308/3,308 with the same ID set as the full run,
including all six restore/isolation guard tests. Both runtime aliases retain
server-side binding; the previously documented offline administrative literals
are outside the runtime claim. This is synthetic evidence, not review approval.

**Execution and cleanup.** New disposable PG16.14 and Redis7; the verified
Redis 7.4.11 binary was on PATH, broker gate and Poppler required, umask 022.
On the lane cluster only, fsync was disabled at 2026-11-24T20:52:13.994305Z
after sustained I/O pressure and DataFileImmediateSync waits; cluster removed
by 2026-11-25T00:51:31+00:00. No disabled truncate guards (126/126); no other client
backends. Both named volumes/containers absent, ephemeral loopback ports
rebound and released. Publication uses distinct code and evidence-only refs,
`ulw/cpi-h-kek-binding` and `ulw/cpi-h-kek-binding-evidence-sc5`, so the retained manifest does not change the code SHA.

## Historical `76376101` record

**Status: pushed, independently read back, and lane resources removed.**
Branch `ulw/cpi-h-kek-binding`, based on `4f8d2454987ca1d083ead53a7501126d647a89bc`.
First candidate: `76376101614f209d9d7b4ab4a74ac87fdca4f832`.
Synthetic data only; no live readiness, hosted CI, or external service claim.

## Baseline and scope of the evidence

The baseline Python encryption calls already parameterize the SQL. The secret
store supplies the KEK to `apps/tenancy/envelope.py` (`_kek`, `_call_scalar`,
tenant/protected encrypt/decrypt, issue, blind-index and rewrap paths).
`apps/comms/inbox_inbound.py::inbound_context` uses a proof in both a resolver
call and a transaction-local GUC write. The baseline Django connection builders
set `prepare_threshold=None`, but not `server_side_binding=True`. On the pinned
Django 5.2.17 / psycopg 3.3.4 this selects `Cursor` → `psycopg.ClientCursor`,
**not** `ServerBindingCursor` → `psycopg.Cursor`.

No historical KEK was extracted, hashed or included in evidence. The baseline
claim is a source/driver trace, backed by a controlled client-binding mutation:
the second authenticated `clinic_app` connection checked for an **exact
placeholder template**, never retrieved the other connection's SQL. This is a
public-canary contrast, **not** an observation or hash of a leaked KEK.

## Change and query inventory

* `config/settings/database.py::postgres_options()` produces fresh options:
  server-side binding **on**, named prepared statements still **off**, same
  search path. Base uses it for default and the separately built locks alias;
  dev inherits base. Test and renewal use it before copying the aliases. The
  strict parser supplies prod default/locks/agent, release owner, browser
  default/lock copy. The non-TLS agent constructor uses it too. The realtime
  settings inherit the selected parent; lock factories inherit their named
  alias. No `ATOMIC_REQUESTS`, cursor pooling, or transaction-lifetime change.
* Application envelope, webhook resolver and webhook `set_config` SQL
  themselves are **unchanged**. No function rewrites, role impersonation or
  changes to `apps/comms` send outcomes.
* `tests/identity/test_identity_acl_migration.py`: replace the two
  parameterized `SET app.current_*` statements with one scalar select of
  two `set_config(..., value, false)` calls. False preserves the original
  **session**, not transaction, lifetime.
* `tests/scribe/test_draft_authority.py`: type the polymorphic JSON builder's
  citation ID as `text`; assert the **actual** `invalid draft source`
  integrity error, rather than accepting any driver/database error.
* `ops/db/bootstrap.sql`: revoke ordinary SET on the protected logging GUCs;
  apply zero non-error and on-error limits and `terse` verbosity as **login
  defaults** to the canonical app/owner/agent and existing optional machine
  logins. `ops/testing/https_role_bootstrap.py` installs the same defaults
  for the HTTPS stack's three fresh, separately created logins.
* `ops/db/posture.py`: verify fresh-session effective settings, supported
  stderr/syslog destinations, absence of protected SET privileges, and
  exact applicable global/role/database settings (including separately
  provisioned machine logins). New logins need bootstrap + posture. Existing
  pool/server sessions must reconnect. CSV/JSON preserve separate error
  context fields and are explicitly **not** accepted by this posture.
* Settings, HTTPS-bootstrap and pooling tests pin the resulting option maps
  and catalog rows. `tests/infra/test_bound_parameters.py` compares nine
  exact templates: public canary, both tenant-context setters, tenant
  encrypt/decrypt, protected encrypt/decrypt, webhook resolver, webhook
  proof/scope setter. The writer waits for the reader's result before its
  next SQL; synchronization uses an Event and a joined executor. The reader
  gets only a boolean for a uniquely tagged, idle or idle-in-transaction
  backend of the same database/role. The test never emits query bodies.

### SQL statement checklist for `76376101`

There were **two binding-compatibility call sites**, both in tests.
No production envelope or webhook query text was changed.

| Site | Changed or added SQL |
|---|---|
| Identity migration fixture | Replaced `SET app.current_user_id = %s` and `SET app.current_tenant = %s` with one `SELECT set_config('app.current_user_id', %s, false), set_config('app.current_tenant', %s, false)`. |
| Scribe citation guard fixture | Its `INSERT … SELECT` now uses `jsonb_build_object('kind','transcript','id',%s::text)` in `jsonb_build_array`; only the citation ID argument changed. |
| Main bootstrap | Added `REVOKE SET ON PARAMETER log_parameter_max_length, log_error_verbosity` for PUBLIC and the three named login roles. A `SELECT format(...)` over matching login names emits three `ALTER ROLE … SET` statements: `log_parameter_max_length=0`, `log_parameter_max_length_on_error=0`, `log_error_verbosity=terse`. |
| HTTPS bootstrap | Added the same three `ALTER ROLE … SET` statements to the existing bootstrap batch, for each of its app, agent and owner logins. |
| Deployment posture | `POSTURE_QUERY` adds sensitive `SET` privilege checks and the stderr/syslog destination check. New `MACHINE_LOGINS_QUERY` selects optional machine logins. `EFFECTIVE_SETTINGS_QUERY` adds all three logging GUCs to its `current_setting` results. Existing `ROLE_SETTINGS_QUERY` text is unchanged; its bound role list now includes the machine logins. |
| Pooling and HTTPS tests | Pooling adds the same three `current_setting` results and passes the expanded role set to the existing catalog query. HTTPS adds `SELECT rolname, rolconfig FROM pg_roles WHERE rolname = ANY(%s)`. |
| Binding regression | Adds `SELECT %s::text` (public canary), and one boolean-only equality check of the expected statement template in `pg_stat_activity`, restricted to the uniquely tagged same-role test backend. The nine templates are *expectations* for actual calls, not nine rewritten production statements. |
| Privilege regression | Adds `SELECT session_user, current_user`; two denied attempts (`SET log_parameter_max_length = -1`, `SET log_error_verbosity = verbose`); one rollback-only successful `SET LOCAL log_parameter_max_length_on_error = -1`; and a `SELECT current_setting(...)` of the non-error limit, on-error limit and server verbosity. |

## PostgreSQL control proof

Actual test server: PostgreSQL **16.14**, Python 3.12.3. `clinic_app` saw
`log_parameter_max_length=0` (context `superuser`),
`log_parameter_max_length_on_error=0` (`user`), and `log_error_verbosity=terse`
(`superuser`). The GUCs **do not have identical privileges**. Separate fresh
runtime connection: raising the non-error limit or server verbosity fails
with SQLSTATE `42501`; `SET LOCAL` of the on-error limit to `-1` succeeds and
is rolled back. The latter cannot honestly be tested as permission denied.
The compensating control excludes bind context from supported log destinations.
Deliberate privileged changes to those controls, untrusted extensions, and
queries explicitly emitting argument values are outside this core bind-logging claim.

**Who can affect whom:** two concurrent, independently authenticated, distinct
`clinic_app` backends reported on-error limits `(0, 0)`. A `SET LOCAL` of
one byte in the first yielded `(1B, 0)`; a third fresh login still reported
`0`. After rollback the pair was `(0, 0)` again. This proves isolation of
session `SET`, **not** immutability of login defaults: a rollback-only
`ALTER ROLE CURRENT_USER SET ... = 0` also succeeded. A committed change to
a USERSET role default could affect *future* same-role logins, not an
already-open backend. Even then both the protected non-error limit and
protected server-side `terse` control still apply. Posture rejects changed
defaults. See PostgreSQL 16 manual sections *ALTER ROLE*, *Error Reporting
and Logging* and *System Administration Functions*.

**Reading logs is a different privilege:** on the actual instance,
`clinic_app` was not superuser/CREATEROLE/BYPASSRLS and had **zero** other
role memberships/SET targets. `MEMBER`, `USAGE` and `SET` were false for
`pg_read_server_files`, `pg_execute_server_program`, `pg_write_server_files`,
`pg_read_all_settings` and `pg_monitor`. Effective `EXECUTE` was false for
**all** overloads of `pg_read_file` (4), `pg_read_binary_file` (4),
`pg_ls_logdir` (1) and `lo_import` (2). No foreign tables were installed.
Actual destination was external stderr, in-server collector off. An ordinary
SQL login does not acquire the Docker/host log reader. These assertions
checked **effective ACLs**, not the contents of any log file. This is not
a claim about someone separately granted host/root/central logging access.
The separate operator-only harmless log-canary test below establishes that
bind values were not written; it does not rely merely on lack of log access.

**Separate administrative follow-up, not protected by this Django change:**
`ops/testing/restore_verification.py::{require_key_probe,verify_object_store}`
construct escrow-key literals for the offline `ContainerPostgres.sql` psql
transport (`clinic_super`, not a Django `clinic_app` cursor). Core bind-detail
limits do not redact literals. A separate transport change should pass keys
through an extended-protocol bind inside the owned restore stack. This lane
did not change restore authority/protocol or claim that restoring via psql
protects the admin query text. No escrow key was retrieved.

A disposable-cluster operator changed `log_statement`, the duration threshold,
the duration-sampling threshold, and transaction sample rate while the
application held a transaction. The typed public probe ran in that transaction
and the next; enabled modes actually produced log records. A deliberate
division-by-zero with a harmless parameter and runtime on-error limit `-1`
actually produced an error log. No canary value occurred in server logs. Each
operator setting was reset. **Public log-canary SHA-256** (not a key):
`ce7820f1578c04d0f02316c6967d819e7d72d5eece66591e29469253d585fc8e`.

## Gate receipts (UTC)

All long runs were waited on through `systemd-run --user --wait --collect`
units `cpi-kek-*` with `Nice=10`. One DB pytest at a time. Container IDs and
credentials are not needed to reproduce the test suite and are omitted.
Only `cpi-kek-pg-01a20d6b` (internal port 55432, published loopback 33294)
and `cpi-kek-redis-01a20d6b` (56379 → loopback 33295) were started.
The shared host showed full IO pressure ~29% over 10 seconds. **Only** the
disposable PG container started with `fsync=off` at
`2026-11-24T12:08:22.535867Z`; confirmed as the runtime role.

* `make db-bootstrap`, `make migrate`, `make db-posture`: exit 0. Fresh install.
* `pytest --reuse-db -q tests/infra/test_bound_parameters.py tests/realtime/test_pooling.py tests/infra/test_agent_database.py tests/infra/test_database_options.py tests/identity/test_identity_acl_migration.py tests/scribe/test_draft_authority.py tests/identity/test_tenant_guc_boundaries.py`: 49 passed, exit 0.
* `pytest --reuse-db -q tests/infra/test_https_role_bootstrap.py tests/infra/test_db_posture_url.py tests/scribe/test_draft_authority.py::test_raw_citation_and_history_guards_reject_foreign_references tests/isolation/test_backup_restore_guard.py`: 14 passed, exit 0.
* Final nine-template test, role defaults and typed scribe guard: `pytest --reuse-db -q tests/infra/test_bound_parameters.py tests/realtime/test_pooling.py tests/scribe/test_draft_authority.py::test_raw_citation_and_history_guards_reject_foreign_references`: 8 passed. The external mutation patched only the producer's options at pytest call time (after encrypted fixture setup), reopened it, and reran the same binding node. **1 failed**, exit 1 as required, first mismatch `SELECT $1::text`; no encryption/proof operation executed on the mutated connection.
* Candidate `76376101`, `cpi-kek-static-76376101`, start `2026-11-24T12:27:28Z`: `ruff check .`, `ruff format --check .` (2,948 files), `mypy .` (strict, 2,948 files), `manage.py check`, `manage.py makemigrations --check --dry-run`, `make db-posture`, `git diff --check 4f8d2454..HEAD`: all exit 0.
* Invalid preliminary full-run launchers are **not** counted: systemd expanded a shell array before the first invocation; xunit2 caused warnings-as-errors with `record_property` in the second. Each was stopped, verified quiescent, and the lane's test DB was rebuilt from bootstrap/migrations. **126/126** expected guards enabled before rebuild and after, none missing or disabled.
* First completed exact-commit non-browser run, `2026-11-24T12:32:44Z` to `16:11:03Z`, exit 1: **16,577 passed, 38 setup errors, 3 failed, 0 skipped**, 93.43% coverage. The two new regression tests, 14 recovery tests, 6 restore/isolation guard tests and HTTPS bootstrap test all passed. The 38 setup errors required `redis-server` on `PATH`; an endpoint alone does not provide the fairness fixture executable. The 3 harness tests try to change ordinary file/directory modes to 0600/0700, but with a process umask of 077 those mutations did nothing. Independent fake-only harness likewise reported 3 failed / 3,305 passed. All 126 truncate guards remained enabled at suite exit; no application SQL incompatibility appeared in this run.
* Corrected **test environment only**, not source: supplied a lane-private Redis 7.4.11 executable from the local official `redis:7` image (binary SHA-256 `acf84fdf1c977253714617a7470b0acf5237c4b0bc1b36c3842b332c8dce491b`). Logs and workspace roots remain private (0700 directories); pytest inherits ordinary umask 022 so mode-drift mutators exercise actual changes. All 41 affected cases selected from the previous JUnit and then the complete exact-SHA suite, plus the independent fake-only isolation harness, were rerun.
* Corrected environment: **41/41 passed** in 65.14 s with no skips/errors; **126/126** guards enabled. Independent ops/testing fake-only harness `pytest -q -p no:cacheprovider --basetemp=<private> -o junit_family=xunit1 --junitxml=<private>/isolation-r2.xml tests/isolation`: **3,308/3,308 passed** in 365.64 s; no failures, errors or skips. The earlier `session-privilege` scratch proof expected `1` rather than PostgreSQL's actual formatted `1B`; corrected display expectation, `session-privilege-r2` exit 0, no source change.
* Independent collection on clean tree `e6ca823b183ff1f3e7dd6c5bcafa0a1ff4ec994a` at candidate SHA: `pytest --collect-only -qqq -p no:cacheprovider -p independent_ids --ignore=tests/renewal/browser tests`, exit 0, 16,618 distinct full pytest node IDs and 16,618 distinct `(file, classname, name)` JUnit identities. No raw IDs retained in the claim. The final oracle compares **multisets**, not just counts, including class and parameter variants; preliminary red runs do not contribute results.
* Final exact-commit gate `cpi-kek-final-gate-76376101`, Nice=10, systemd invocation `461b24c0b13e4688986ae8535ea69f18`: `ExecMainCode=CLD_EXITED`, `ExecMainStatus=0`, `Result=success`. From **2026-11-24T16:15:22+00:00** through **2026-11-24T19:54:59+00:00**, on `76376101614f209d9d7b4ab4a74ac87fdca4f832`, clean tree `e6ca823b183ff1f3e7dd6c5bcafa0a1ff4ec994a` at entry and unchanged at exit. Command: `python -m pytest -q --tb=short --reuse-db --ignore=tests/renewal/browser -o junit_family=xunit1 -p lane_report <all 26 --cov targets from ops/testing/coverage-targets.txt> --cov-fail-under=90 --cov-report=term:skip-covered --junitxml=<private>/final-gate-76376101.xml tests`. `lane_report` records only failed test locations and error types, never alters selection or outcomes.
* **16,618 passed; zero failures, errors or skips**. Independently collected vs executed: **16,618/16,618**; missing 0, extra 0, duplicate 0, including parameter variants. Sorted reference and JUnit execution hashes are both `836686f0fc5dc51a8ecca3260d316ca37ea87ba6e26b501c0f404f4dd433b9dd`; raw-node-ID-set fingerprint `55ab491f6fdb810195aa009dc3d6d70b8aca1d1d68dcb6940c028b5075c1b640`. Coverage **93.59%**. Archive SHA-256 for the raw JUnit (deleted rather than retaining possibly sensitive properties): `22c031839be63ba633ce97f9ee0244fbba887bd0a776ebfcb346aff04ad6449e`. All 126/126 teardown guards enabled at start and finish; zero other client backends at cleanup.
* Publication: ordinary (non-forced) `git push origin 76376101614f209d9d7b4ab4a74ac87fdca4f832:refs/heads/ulw/cpi-h-kek-binding` exit 0. `git ls-remote --exit-code --heads origin refs/heads/ulw/cpi-h-kek-binding` returned `76376101614f209d9d7b4ab4a74ac87fdca4f832` at `2026-11-24T19:55:11+00:00` and again after removing the worktree.
* Cleanup: both ID/label/mount-checked containers, the named PG volume and Redis anonymous volume removed; exact name, label and volume absence verified. No unrelated containers, settings or ports touched. Local loopback ports 33294 and 33295 each rebound and released. Disposable `fsync=off` ended by **2026-11-24T19:55:14+00:00**. Local scratch, temporary executables, venv/caches and the lane worktree were absent by **2026-11-24T19:57:30+00:00**. The code branch and this redacted evidence are retained intentionally.
* `KEK-BINDING-RECEIPTS.json` contains the final full-unit status, historical JUnit counts/hashes (including interrupted/failed attempts), independent reference comparison, effective SQL ACLs, committed-tree identifiers and closure receipts. This was an exact-commit synthetic non-browser gate, not a browser, live-data, external audit, or hosted CI approval.

Frozen-plan SHA-256 (untouched): `1cb6d1702dc8960b7bccced4c69a0193418d22d29a521498b083eb6c891dff19`.

## Verification unit exit inventory

Earlier transient units used `--collect`; where a numeric success status was no longer cached, its basis is stated rather than inventing a systemd receipt. The final unit was explicitly held through exit. `—` means no JUnit was generated (not a substitute XML).

| Unit | Exit | JUnit | Basis |
|---|---:|---|---|
| `cpi-kek-candidate.service` | 0 | — | manager journal (no failed main exit) and completed output |
| `cpi-kek-collect-exact.service` | 0 | — | waited runner/command receipt |
| `cpi-kek-deps.service` | 0 | — | waited runner/command receipt |
| `cpi-kek-final-gate-76376101.service` | 0 | recheck.xml, final-gate-76376101.xml | held systemd ExecMainStatus/Result |
| `cpi-kek-full-76376101.service` | 2 | full.xml | systemd journal main EXIT_STATUS |
| `cpi-kek-gate-76376101.service` | 1 | gate-76376101.xml | systemd journal main EXIT_STATUS |
| `cpi-kek-isolation-76376101.service` | 1 | isolation.xml | systemd journal main EXIT_STATUS |
| `cpi-kek-isolation-r2.service` | 0 | isolation-r2.xml | manager journal (no failed main exit) and completed output |
| `cpi-kek-mutation.service` | 0 | — | manager journal (no failed main exit) and completed output |
| `cpi-kek-posture-target.service` | 0 | — | manager journal (no failed main exit) and completed output |
| `cpi-kek-precheck.service` | 1 | — | systemd journal main EXIT_STATUS |
| `cpi-kek-provision.service` | 0 | — | manager journal (no failed main exit) and completed output |
| `cpi-kek-server-log-proof.service` | 0 | — | manager journal (no failed main exit) and completed output |
| `cpi-kek-session-privilege-r2.service` | 0 | — | waited runner/command receipt |
| `cpi-kek-session-privilege.service` | 1 | — | systemd journal main EXIT_STATUS |
| `cpi-kek-static-76376101.service` | 0 | — | manager journal (no failed main exit) and completed output |
| `cpi-kek-suite-76376101.service` | 1 | suite-76376101.xml | systemd journal main EXIT_STATUS |
| `cpi-kek-target.service` | 0 | — | waited runner/command receipt |
| `cpi-kek-publish-audit.service` | 0 | — | final publication seal and completed manager journal, no failed main exit; the unit wrote the other receipts before its own exit |

The precommit `precheck` exit 1 was Ruff E501 on a test assertion (89 columns
for an 88-column limit), fixed before this SHA. The invalid preliminary suite
also observed a duplicate synthetic principal during setup after an interrupted
run; its database was rebuilt, not reused for certification. Neither failure
was suppressed or included as a pass.

**Independent post-cleanup readback:** the remote branch still pointed to the
full SHA above after the publisher exited. `systemctl --user list-units --all
'cpi-kek-*'` and the lane container/volume checks were empty, and neither
private scratch root nor the lane worktree existed. The publisher's terminal
journal seal for `KEK-BINDING-RECEIPTS.json` matched its independently computed
SHA-256: `825407de46490109d368aac8fab495f7a8423158699821539755d89723a60d63`.
