"""Frozen SQL for invitation claim type."""

from ._lifecycle_authority_sql import CLAIM_REPAIR

SQL = "SET LOCAL ROLE clinic_resolver;\n" + CLAIM_REPAIR + "\nRESET ROLE;"
