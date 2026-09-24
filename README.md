# Clinic OS

A web workspace for Brazilian outpatient clinics, built with Django 5.2,
PostgreSQL 16, server-rendered templates, and HTMX.

Product naming is pending: a `Clinic_Ops_SVG/` brand pack exists in the main
checkout (not this worktree) with the tentative name "Clinic Ops". Until the
owner approves the rename, the shell wordmark, installable manifest and icon
alt text carry "Clinic Ops" while code identifiers, routes, static paths,
page titles and other copy keep "Clinic OS".

The current implementation is **synthetic-data only**. The Phase 1A
foundation ships organization/clinic identity, staff roles, TOTP, tenant
isolation, an append-only audit ledger, patient registration/search,
practitioner availability, booking, day/week agendas, rescheduling, and
cancellation.

The renewal domains are implemented and verified in synthetic mode: patient
contacts, invitations and sessions, versioned questionnaires, self-booking,
waitlist and reminders, clinical encounters and longitudinal records,
quarantined attachments, amendments, retention policies and legal holds,
versioned consent, teleconsultation, physician verification, prescription
drafts, PDF/QR documents, signing and public verification, PIX billing and
receipts, and bounded clinic configuration. Every provider-backed slice runs
on a labelled synthetic adapter and stays `waiting_external` until its
capability record gains an approved provider and sandbox; see the
[capability register](docs/integrations/capabilities.md).

Three service entrypoints remain deferred stubs
(`NotImplementedError("Phase >=1")`): `prescription.issue_prescription`,
`retention.apply_retention_policy`, and `interop.exchange_clinical_record`.
Live use remains subject to the
[live-data gate](docs/compliance/LIVE-DATA-GATE.md).

## Work on the project

Use Python 3.12 or 3.13, `uv`, Docker Compose, and PostgreSQL 16. Follow the
[runbook](docs/RUNBOOK.md) for the database roles, environment, migrations,
local startup sequence and the renewal verification runner, then the
[contribution guide](docs/CONTRIBUTING.md) for required checks.

- [Architecture and module status](docs/ARCHITECTURE.md)
- [Security boundaries](docs/SECURITY.md)
- [Current design system](DESIGN.md)
- [Renewal roadmap](docs/plans/clinic-os-renewal-roadmap.md)
- [Integration capability register](docs/integrations/capabilities.md)
- [Repository cleanup record](docs/maintenance/2026-09-12-omo-cleanup.md)

## Planning records

The original `initial_plan_en.md` and `initialreport.md`, when present locally,
are historical inputs. Their regulatory, vendor, pricing, and delivery
assumptions need revalidation before reuse.

Plans and drafts under `.omo/` are local workflow artifacts. The tracked
[approved Phase 1A plan](docs/plans/clinic-os-phase1a-approved.md) and its
SHA-256 sidecar are frozen CI inputs. Preserve their bytes when writing a new
product roadmap. Remaining `.omo/evidence` and runner inputs also retain
shared-worktree and verification responsibilities described in the cleanup
record.
