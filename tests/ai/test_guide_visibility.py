"""The guide consent predicate cannot disclose a bit outside ordinary guide RLS."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.insurance.models import Guide
from django.db import connection

from ai.test_guide_consent import _binding
from ai.test_revenue_cycle import finance_setup, world
from billing.journal_support import finance_scope

if TYPE_CHECKING:
    from ai.revenue_support import RevenueWorld

__all__ = ("finance_setup", "world")
pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("channel", ["scim_token", "sso_connection", "support_grant"])
def test_guide_bit_requires_the_same_staff_scope_as_the_visible_guide(
    world: RevenueWorld, channel: str
) -> None:
    setup, guide = world.setup, world.cycle.guide
    unknown = uuid4()
    with finance_scope(setup), connection.cursor() as cursor:
        assert Guide.objects.filter(pk=guide.pk).exists()
        assert _binding(setup.clinic_id, guide.pk, guide.encounter_id, None)
        cursor.execute(
            "SELECT set_config(%s,%s,true)",
            ["app.current_" + channel, str(uuid4())],
        )
        assert not Guide.objects.filter(pk=guide.pk).exists()
        assert not Guide.objects.filter(pk=unknown).exists()
        # Verify the dependency on the deployed guide policy before exercising
        # a nonstaff context. On the old installation no restricted bit is read.
        cursor.execute("""
            SELECT EXISTS (
              SELECT 1 FROM pg_policy p JOIN pg_depend d
                ON d.classid='pg_policy'::regclass AND d.objid=p.oid
              WHERE p.polrelid='clinic_app.insurance_guide'::regclass
                AND p.polname='insurance_scope'
                AND d.refclassid='pg_proc'::regclass
                AND d.refobjid=to_regprocedure(
                  'clinic_app.insurance_guide_read_scope(uuid,uuid)'))
        """)
        assert cursor.fetchone() == (True,)
        decisions = tuple(
            _binding(setup.clinic_id, selected, guide.encounter_id, None)
            for selected in (guide.pk, unknown)
        )
        assert decisions == (False, False)
