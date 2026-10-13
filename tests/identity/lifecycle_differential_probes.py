"""Real public signup and pending-account enrollment, not staff exemptions."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING
from uuid import uuid4

from apps.identity.invitations import (
    InvitationEnrollment,
    enroll_invitation,
    invitation_token,
    invite_staff,
)
from apps.identity.models import UserClinicRole
from apps.identity.signup import SignupCapture, submit_signup
from apps.tenancy.db import tenant_context
from django.db import connection, transaction

from auth.stepup_test_support import create_role_actor
from identity.nonstaff_differential import DifferentialProbe
from otp_test_support import fixed_otp_time, runtime_role, token_for
from rbac_fixtures import RBAC_RAW_CREDENTIAL

if TYPE_CHECKING:
    from collections.abc import Iterator

    from rbac_fixtures import RbacGraph


@contextmanager
def public_context() -> Iterator[None]:
    """Use the actual public entrypoint context; restore every caller GUC."""
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT current_setting('app.current_tenant',true), "
            "current_setting('app.current_user_id',true), "
            "current_setting('app.current_patient_session',true), "
            "current_setting('app.current_principal',true)"
        )
        saved = cursor.fetchone()
        assert saved is not None
        cursor.execute(
            "SELECT set_config('app.current_tenant','',true), "
            "set_config('app.current_user_id','',true), "
            "set_config('app.current_patient_session','',true), "
            "set_config('app.current_principal','',true)"
        )
        try:
            yield
        finally:
            if not connection.needs_rollback:
                cursor.execute(
                    "SELECT set_config('app.current_tenant',%s,true), "
                    "set_config('app.current_user_id',%s,true), "
                    "set_config('app.current_patient_session',%s,true), "
                    "set_config('app.current_principal',%s,true)",
                    [value or "" for value in saved],
                )


def lifecycle_probes(graph: RbacGraph) -> list[DifferentialProbe]:
    """Prepare genuine capabilities before observing their final invocation."""
    manager = create_role_actor(graph, UserClinicRole.Role.CLINIC_ADMIN)
    with runtime_role(), tenant_context(manager.pk, graph.organization_a):
        invitation = invite_staff(
            clinic_id=graph.clinic_a,
            email=uuid4().hex + "@example.invalid",
            bundle=UserClinicRole.Role.RECEPTIONIST,
        )
    token = invitation_token(invitation)
    username = "synthetic-enrollment-" + uuid4().hex
    # Random hex credentials can be too similar to UUID usernames or emails.
    password = RBAC_RAW_CREDENTIAL
    prepared: list[InvitationEnrollment] = []

    def prepare_enrollment() -> None:
        with public_context():
            prepared[:] = [
                enroll_invitation(token=token, username=username, password=password)
            ]
        assert prepared[0].accepted_user_id is None

    def accept_enrollment() -> bool:
        with public_context(), fixed_otp_time():
            enrolled = enroll_invitation(
                token=token,
                username=username,
                password=password,
                otp_token=token_for(prepared[0].device),
            )
        return enrolled.accepted_user_id == invitation.pending_user_id

    def signup() -> bool:
        nonce = uuid4().hex
        with public_context():
            receipt = submit_signup(
                capture=SignupCapture(
                    organization_name="Synthetic pending organization",
                    cnpj="11222333000181",
                    legal_responsible="Synthetic responsible",
                    username="synthetic-signup-" + nonce,
                    email=nonce + "@example.invalid",
                    password=RBAC_RAW_CREDENTIAL,
                ),
                idempotency_key=uuid4(),
            )
        return receipt.created

    return [
        DifferentialProbe(
            "apps.identity.invitations.enroll_invitation",
            accept_enrollment,
            prepare=prepare_enrollment,
        ),
        DifferentialProbe("apps.identity.signup.submit_signup", signup),
    ]
