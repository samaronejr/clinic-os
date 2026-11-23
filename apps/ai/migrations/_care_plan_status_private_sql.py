"""Frozen SQL for care plan status private."""

SQL = """
            SET LOCAL ROLE clinic_resolver;
            REVOKE ALL ON FUNCTION
                clinic_app.ai_care_plan_current(uuid,integer)
                FROM PUBLIC,clinic_app,clinic_owner,clinic_agent;
            RESET ROLE;
            """
