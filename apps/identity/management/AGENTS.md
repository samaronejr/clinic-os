# OWNER LIFECYCLE OPERATIONS

## OVERVIEW
Interactive owner-role commands and guarded lifecycle services; score 8 for a separate privileged mutation domain.

## WHERE TO LOOK
| Task | Location | Notes |
|------|----------|-------|
| Command argument adapters | `commands/` | Bootstrap, provision, revoke, change timezone |
| Owner database/TTY gate | `base.py` | `OwnerDatabaseCommand`, hidden input, payload-free failures |
| Existing-tenant command protocol | `owner_command.py` | Operator/organization/clinic selection and TOTP preflight |
| Bound operator context | `context.py` | `LifecycleContext`, temporary role/GUC transitions |
| Initial clinic creation | `bootstrap.py` | Initial identity graph and bootstrap audit |
| Staff provisioning | `provisioning.py` | Canonical identifiers and identity lock gates |
| Role removal | `revocation.py` | Last-owner and active-practitioner guards |
| Empty-clinic timezone change | `timezone_change.py` | Clinic gate and dependency check |
| Management authenticator flow | `totp.py` | Confirmed-device selection and token verification |
| Regression coverage | `../../../tests/test_owner_command_context.py`, `test_management_totp.py`, `test_lifecycle_*.py` | Privileged context and secret handling |

## CONVENTIONS
- Commands require the `clinic_owner` database role; runtime owner authorization is checked separately.
- Open `/dev/tty` before database access; secrets use hidden input, not ordinary stdin.
- Existing-tenant commands bind explicit operator, organization and clinic UUIDs.
- Confirmed-device preflight precedes token entry; token verification commits separately from the mutation.
- Scoped lifecycle GUC helpers require an active transaction and preserve prior context.
- `assume_runtime_owner` temporarily adopts `clinic_app` to verify the bound active clinic owner.
- Restore prior role/GUC values unless the connection is already marked for rollback.
- Role revocation locks the clinic and target user before checking/removing membership.
- Physician revocation rejects non-retired availability or future scheduled appointments.
- Timezone changes return early for the existing zone; otherwise durable dependencies block them.

## ANTI-PATTERNS
- Do not pass passwords or TOTP codes in command-line arguments.
- Do not mistake database ownership for the operator's clinic-owner membership.
- Do not remove the last active owner from a clinic.
- Do not bypass lifecycle context restoration when adding administrative SQL.
- Do not echo rejected lifecycle inputs; command failures deliberately expose a single safe message.
