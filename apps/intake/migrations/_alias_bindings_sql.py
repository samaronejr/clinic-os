"""Least-privilege mutation columns and non-deletable alias bindings."""

SQL = """
GRANT SELECT ON TABLE clinic_app.intake_mergedecision,
    clinic_app.intake_patientalias,clinic_app.intake_patientmergetransfer,
    clinic_app.identity_sessionrecord,clinic_app.intake_duplicatecandidate
    TO clinic_resolver;
GRANT SELECT (demographics_id,actor_id)
    ON TABLE clinic_app.intake_demographicscorrection TO clinic_resolver;
GRANT UPDATE (state) ON TABLE clinic_app.intake_duplicatecandidate
    TO clinic_app,clinic_resolver;
GRANT UPDATE (active,deactivated_at,deactivated_by_id,deactivation_session_id,
    deactivation_reason) ON TABLE clinic_app.intake_patientalias TO clinic_app;

DROP TRIGGER intake_duplicatecandidate_retained ON clinic_app.intake_duplicatecandidate;
CREATE TRIGGER intake_duplicatecandidate_retained
BEFORE DELETE ON clinic_app.intake_duplicatecandidate
FOR EACH ROW EXECUTE FUNCTION clinic_app.intake_duplicate_history_guard();
CREATE TRIGGER intake_duplicatecandidate_transition
BEFORE UPDATE ON clinic_app.intake_duplicatecandidate
FOR EACH ROW EXECUTE FUNCTION clinic_app.intake_duplicate_transition_guard();
CREATE TRIGGER intake_merge_decision_binding
BEFORE INSERT ON clinic_app.intake_mergedecision
FOR EACH ROW EXECUTE FUNCTION clinic_app.intake_merge_decision_guard();
CREATE TRIGGER intake_merge_decision_retained
BEFORE UPDATE OR DELETE ON clinic_app.intake_mergedecision
FOR EACH ROW EXECUTE FUNCTION clinic_app.intake_duplicate_history_guard();
CREATE CONSTRAINT TRIGGER intake_merge_decision_receipt
AFTER INSERT ON clinic_app.intake_mergedecision DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION clinic_app.intake_merge_receipt_guard();
CREATE TRIGGER intake_patient_alias_binding
BEFORE INSERT OR UPDATE ON clinic_app.intake_patientalias
FOR EACH ROW EXECUTE FUNCTION clinic_app.intake_patient_alias_guard();
CREATE TRIGGER intake_patient_alias_retained
BEFORE DELETE ON clinic_app.intake_patientalias
FOR EACH ROW EXECUTE FUNCTION clinic_app.intake_duplicate_history_guard();
CREATE TRIGGER intake_patient_merge_transfer_retained
BEFORE UPDATE OR DELETE ON clinic_app.intake_patientmergetransfer
FOR EACH ROW EXECUTE FUNCTION clinic_app.intake_duplicate_history_guard();
"""


def composite_bindings() -> str:
    """Keep every new identity edge inside its declared organization."""
    relations = {
        "intake_mergedecision": ("merged_patient_id", "survivor_patient_id"),
        "intake_patientalias": ("merged_patient_id", "survivor_patient_id"),
    }
    result = ""
    for table, columns in relations.items():
        for column in columns:
            result += f"""
ALTER TABLE clinic_app.{table}
ADD CONSTRAINT {table}_{column}_org_fk
FOREIGN KEY (organization_id,{column})
REFERENCES clinic_app.intake_patient(organization_id,id) NOT DEFERRABLE;
"""
    for table in ("intake_mergedecision", "intake_patientmergetransfer"):
        result += f"""
ALTER TABLE clinic_app.{table}
ADD CONSTRAINT {table}_clinic_org_fk
FOREIGN KEY (organization_id,clinic_id)
REFERENCES clinic_app.identity_clinic(organization_id,id) NOT DEFERRABLE;
"""
    for table in ("intake_patientalias", "intake_patientmergetransfer"):
        result += f"""
ALTER TABLE clinic_app.{table}
ADD CONSTRAINT {table}_decision_org_fk
FOREIGN KEY (organization_id,merge_decision_id)
REFERENCES clinic_app.intake_mergedecision(organization_id,id) NOT DEFERRABLE;
"""
    return result
