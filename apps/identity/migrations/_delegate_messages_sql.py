"""Let a verified representation carry the portal ``messages`` operation."""

SQL = """
ALTER TABLE clinic_app.identity_patientdelegategrant
 DROP CONSTRAINT delegate_operations_check,
 ADD CONSTRAINT delegate_operations_check CHECK (
   coalesce(cardinality(operations) BETWEEN 1 AND 8
   AND array_position(operations,NULL) IS NULL
   AND operations::text[] <@ ARRAY['enrollment_view','questionnaires','booking',
       'records','consent','teleconsult','billing','messages'],false));
"""

REVERSE_SQL = """
ALTER TABLE clinic_app.identity_patientdelegategrant
 DROP CONSTRAINT delegate_operations_check,
 ADD CONSTRAINT delegate_operations_check CHECK (
   coalesce(cardinality(operations) BETWEEN 1 AND 7
   AND array_position(operations,NULL) IS NULL
   AND operations::text[] <@ ARRAY['enrollment_view','questionnaires','booking',
       'records','consent','teleconsult','billing'],false));
"""
