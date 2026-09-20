# IDENTITY AND AUTHENTICATION

## OVERVIEW
Global users, clinic memberships, authenticated actor resolution and OTP flows; score 14 for the central authorization domain.

## WHERE TO LOOK
| Task | Location | Notes |
|------|----------|-------|
| Canonical identity/role schema | `models.py` | `User`, `Organization`, `Clinic`, `UserClinicRole` |
| Trusted actor and physician catalog | `current_context.py` | GUC-bound actor, exact-clinic roles, restricted catalog |
| Role queries/login organization | `services.py` | Lowest authorized organization selected on login |
| Database-backed authentication | `auth_backends.py` | `ClinicBackend` |
| OTP device and challenge plumbing | `otp.py`, `otp_views.py` | Enrollment, verification, guarded continuations |
| Recent authentication | `stepup.py`, `stepup_views.py` | Device-bound freshness, fail-closed checks |
| Login forms and redirects | `forms.py`, `views.py`, `redirects.py` | Canonical input and safe destination handling |
| HTTP registration | `urls.py`, `debug_urls.py` | Product routes versus debug routes |
| Owner lifecycle | `management/` | Separate guide for interactive administrative mutations |
| Database ACL/timezone helpers | `phase1a_*_migration.py` | Called by schema migrations |
| Regression coverage | `../../tests/test_identity_*.py`, `test_2fa*.py`, `test_stepup*.py` | Role, device, redirect and freshness contracts |

## CONVENTIONS
- Users are global; memberships bind user, organization, clinic and role.
- Manager roles are owner, clinic admin and receptionist; physician is distinct.
- `current_context.py` reloads an active actor through `clinic_app.load_current_user()`.
- Physician labels use the authorized catalog, self identity, then UUID fallback.
- Login replaces `active_org_id` from authoritative membership or clears it.
- OTP verification rotates the password session before binding the confirmed device.
- Privileged view decorators require a safe GET continuation for unsafe methods.
- HTMX authentication redirects use status 204 plus `HX-Redirect`.
- Step-up binds freshness to the exact confirmed device and current user.
- Invalid, boolean or future freshness values fail closed and clear stale state.

## ANTI-PATTERNS
- Do not infer exact-clinic authority from a user's role in another clinic.
- Do not fetch arbitrary users to decorate practitioner displays; use the restricted catalog.
- Do not trust pending, foreign or forged OTP device session identifiers.
- Do not resume a challenged POST by carrying its body into a redirect URL.
- Do not replace owner lifecycle operations with unguarded model writes.
