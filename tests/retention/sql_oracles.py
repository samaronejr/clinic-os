"""Executable contracts for the transient-retention SQL state machine."""

from retention.test_execution import (
    test_legal_hold_receipt_and_crypto_shred_execute_with_the_real_principal,
    test_native_retention_authority_and_restore_keep_independent_subjects,
)

CASES = {
    case.__name__: case
    for case in (
        test_legal_hold_receipt_and_crypto_shred_execute_with_the_real_principal,
        test_native_retention_authority_and_restore_keep_independent_subjects,
    )
}
SQL_PROTOCOL_ORACLES = {
    "clinic_app.retention_audit": list(CASES),
    "clinic_app.retention_audit_scope": list(CASES),
    "clinic_app.retention_authorized": list(CASES),
    "clinic_app.retention_complete": list(CASES),
    "clinic_app.retention_confirm": list(CASES),
    "clinic_app.retention_due": list(CASES),
    "clinic_app.retention_emit_task": list(CASES),
    "clinic_app.retention_failure": list(CASES),
    "clinic_app.retention_held": list(CASES),
    "clinic_app.retention_hold_fence": list(CASES),
    "clinic_app.retention_mutation_allowed": list(CASES),
    "clinic_app.retention_prepare": list(CASES),
    "clinic_app.retention_principal_scope": list(CASES),
    "clinic_app.retention_purge_guard": list(CASES),
    "clinic_app.retention_purge_visible": list(CASES),
    "clinic_app.retention_related_hold": list(CASES),
    "clinic_app.retention_restore_tombstones": list(CASES),
    "clinic_app.retention_schedule": list(CASES),
    "clinic_app.retention_shred": list(CASES),
    "clinic_app.retention_sweep_scopes": list(CASES),
    "clinic_app.retention_system_task_valid": list(CASES),
}
ACTOR_PROTOCOL_ORACLES = dict.fromkeys(
    (
        "clinic_app.retention_principal_scope(uuid,uuid)",
        "clinic_app.retention_purge_visible(uuid)",
    ),
    (
        "retention.test_execution."
        "test_native_retention_authority_and_restore_keep_independent_subjects",
    ),
)
