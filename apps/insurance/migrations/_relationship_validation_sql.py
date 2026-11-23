"""RLS DDL used only within the owner relationship-validation transaction."""


def relax_force_rls(table: str) -> str:
    """Suspend FORCE until deferred foreign keys are validated."""
    return f"ALTER TABLE clinic_app.{table} NO FORCE ROW LEVEL SECURITY"


def restore_force_rls(table: str) -> str:
    """Restore FORCE before the enclosing transaction can commit."""
    return f"ALTER TABLE clinic_app.{table} FORCE ROW LEVEL SECURITY"
