# Bounded clinic configuration

`/clinics/<clinic_id>/settings/` is a TOTP-protected, CSRF-protected native form
for canonical `owner` and `clinic_admin` assignments in that exact clinic.
Receptionists, physicians, unrelated clinics and revoked roles cannot publish.
The shell offers the route only to those roles; the route and PostgreSQL enforce
it independently. No database credential is accepted or rendered.

Configuration is append-only (`ClinicConfiguration`), with a serialized version
and expected-version conflict check. The approved fields are presentation name,
contact email, international telephone, `navy`/`teal` decorative banner token,
optional normalized PNG logo and one reminder lead time from 1/2/6/12/24/48/72
elapsed hours. Branding appears in the staff clinic shell. It does not rename the
canonical clinic identity used in signed records, receipts or message templates.
The fixed white-background banner retains text contrast above 4.5:1 and cannot
style other UI. Logos are decorative, never warning or identity replacements.

Logos reuse the attachment signature/type and scanner boundary, with tighter
256 KiB / 1024-pixel bounds and only static JPEG/PNG input. A successful decode
and PNG re-encode strip metadata before the bytes are stored transactionally.
Rejected or unavailable scans store nothing. The existing synthetic scanner
still rejects live mode; this task does not approve a production scanner.
Authorized staff receive only normalized image/png with no-store and nosniff.

The existing EHR and consent publishers allocate immutable future versions.
Settings expose the four fixed SOAP prompt fields and the existing consent
purpose vocabulary; they reject markup, CSS, executable URLs, extra fields and
permission-shaped input. New publications never rewrite accepted receipts,
finalized documents, signatures or the template references in existing drafts.
Old offered consent tokens retain the existing stale-offer rejection semantics.
Publishing legal/clinical text is not legal/clinical approval.

The appointment trigger reads the latest reminder lead time when creating a
snapshot, defaulting to 24 hours. Existing snapshots, audit actors, timezone,
verified destination and purpose-specific opt-in semantics remain unchanged.
Configuration cannot enable a provider or broaden communication authority.

Timezone is read-only here. The sole mutation path remains
`apps.identity.management.timezone_change.set_clinic_timezone` through the
existing owner-controlled management command and lifecycle context, including
its advisory lock and empty-clinic restrictions. No browser invokes it or gains
owner database authority. Configuration and template publications append fixed,
metadata-only audit events in the same transaction as their new version.

QA: `tests/renewal/test_clinic_settings.py` and registered `clinic-settings`
browser suite, using the task evidence `run_checks.py` disposable database lease.
