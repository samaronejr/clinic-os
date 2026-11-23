"""RLS state transitions for the atomic protected-name backfill."""

DISABLE_RLS_SQL = "ALTER TABLE clinic_app.intake_patient DISABLE ROW LEVEL SECURITY"
ENABLE_RLS_SQL = "ALTER TABLE clinic_app.intake_patient ENABLE ROW LEVEL SECURITY"
FORCE_RLS_SQL = "ALTER TABLE clinic_app.intake_patient FORCE ROW LEVEL SECURITY"
