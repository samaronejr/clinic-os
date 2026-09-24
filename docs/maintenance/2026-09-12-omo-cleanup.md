# OMO cleanup record

Date: 2026-09-12. Scope: obsolete repository-local OpenCode/Lazy OMO
artifacts and documentation, followed by a replacement Clinic OS roadmap.

## Plan recorded before cleanup

1. Authenticate the existing approved-plan pair and run module-boundary tests.
2. Archive the inert paths listed below outside the checkout, preserving file
   bytes and symlinks. Record a manifest and verify it after moving each path.
3. Retain evidence, resource ownership records, and all code still used by CI.
4. Repair stale module documentation; add a repository entry point and a
   product roadmap independent of agent-runtime state.
5. Rerun affected contract tests, static checks, and document-link checks.

No application behavior changes are planned in this pass. Existing regression
tests cover the retained module boundaries and frozen plan; new tests that
merely restate this documentation would add no behavioral coverage.

## Archive selection

The archive root is `../clinic_project-archive/2026-09-12-omo-cleanup/`, relative
to the repository. `manifest.json` records original relative paths, byte
counts, SHA-256 hashes, and symbolic-link targets. It is local recovery
material, outside Git.

| Source | Reason |
| --- | --- |
| `.omo/drafts/` | Superseded planning drafts |
| `.omo/plans/clinic-os-foundation.md` | Historical foundation execution plan |
| `.omo/plans/clinic-os-phase-1a-staff-scheduling-alt.md` | Alternate historical execution plan |
| `.omo/notepads/` | Historical session notes, including unrelated authentication notes |
| `.omo/run-continuation/` | Old OpenCode session continuation records |
| `.omo/start-work/` | Old start-work ledger |
| Three `.omo/boulder.json` backup/truncated files | Inactive damaged/backup task state |
| `.omo/.omo` | Self-referencing symbolic link; archive the link itself |
| `.codegraph` | Link to the old user-level CodeGraph cache; archive the link only |

## Required retained material

- `.omo/evidence/`, including the frozen local approved plan and isolation
  ledger, and the four adjacent execution-host proof files. Two registered
  worktrees share this authority directory. Its ledger still records an open
  historical filesystem claim; archival is not permission to close it or
  remove another worktree's resources.
- `.omo/plans/clinic-os-phase-1a-staff-scheduling.md`:
  `ops/testing/execution_host_preflight.py` still reads this exact path.
- `docs/plans/clinic-os-phase1a-approved.md` and its `.sha256` sidecar:
  immutable inputs authenticated by CI and tests. The new roadmap supersedes
  planning direction, not these historical input bytes.
- `ops/testing/`, browser settings, Makefile targets, tests, and CI workflows:
  these include active isolation and evidence code. References to OMO or
  `/tmp/opencode` alone do not make code unused.
- Application modules, migrations, templates, CSS, `.venv`, user-level plugin
  installations, global CodeGraph data, other worktrees, containers, and volumes.
- The pre-existing untracked `initial_plan_en.md` and `initialreport.md`,
  unchanged as historical source material.

Removing the remaining runtime coupling is proposed in the ulw-plan draft
`.omo/drafts/clinic-os-renewal.md`, with regression coverage and replacement
evidence paths before retirement of old readers. The executable plan will be
created at `.omo/plans/clinic-os-renewal.md` after the skill's approval gate.

## Baseline evidence

- `pytest -q tests/test_phase1a_approved_plan_artifact.py tests/test_module_boundaries.py`:
  21 passed.
- `ruff check .`: passed.
- `ruff format --check .`: 704 files already formatted.
- `mypy .`: no issues in 704 source files.

These commands used the existing `.venv/bin/` executables. They are source and
contract checks, not a database, browser, deployment, or live-data approval.

## Recovery

Consult the archive manifest, ensure no new file occupies an original path,
then move the selected archived entry back to that path. Do not overwrite a
new OMO session or dereference either archived symlink. Compare restored
regular-file hashes and link targets against the manifest.

## Result

Archival completed: 120 regular files (1,068,082 bytes), two symbolic links,
and seven directory entries. A separate read-only verifier confirmed every
archived file hash/size and link target, all ten retained inventory hashes,
both shared worktree links, and the retained preflight/CI plan inputs. The
global CodeGraph cache was not moved.

Documentation now reflects implemented intake and scheduling behavior and
correctly counts eight deferred domains. A root README provides the current
product and contributor entry point. Git ignores the CodeGraph link as well
as a directory and keeps OMX runtime state local.

Post-cleanup verification:

```sh
.venv/bin/python -m pytest -q \
  tests/test_phase1a_approved_plan_artifact.py \
  tests/test_module_boundaries.py \
  tests/test_isolation_tracked_ci_snapshot.py \
  tests/test_isolation_initial_snapshot.py \
  tests/test_makefile_security.py \
  tests/test_makefile_process_security.py
```

Result: 50 passed in 1.71 seconds. A separate read-only check confirmed all
archived and retained hashes, both symbolic links, 23 local documentation
links, and the ulw-plan draft's approval state. `git diff --check` passed.
The baseline Ruff/format/mypy results remain applicable: no Python, browser,
application, migration or CI source was changed.

Full database/browser/container CI was not run for this documentation and
artifact-only cleanup. The old shared resource ledger remains open, as
recorded above; closing that ledger is outside the completed archive scope.
No commit or deployment was made.
