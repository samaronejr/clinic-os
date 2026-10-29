"""Owner-side synthetic setup for the browser's real blinded-review workflow."""

from __future__ import annotations

import json
import os
import sys
from uuid import UUID

import django


def seed(organization: UUID, clinic: UUID, theme: str) -> dict[str, str]:
    """Score through production commands; leave all three reviews to the browser."""
    from apps.identity.models import User  # noqa: PLC0415
    from apps.tenancy.db import tenant_context  # noqa: PLC0415
    from django.contrib.auth.hashers import make_password  # noqa: PLC0415
    from django.db import connection  # noqa: PLC0415

    from ai.evaluation_support import candidate, review_world  # noqa: PLC0415
    from patient_service_support import runtime_role  # noqa: PLC0415
    from renewal.browser._fixture_secrets import new_totp_key  # noqa: PLC0415

    world = review_world(
        organization=organization, clinic=clinic, configuration=candidate()
    )
    result = {"run_id": str(world.run.pk), "model_id": world.configuration.model_id}
    for slot, user_id in (
        ("rater_one", world.reviewers.rater_one),
        ("rater_two", world.reviewers.rater_two),
        ("adjudicator", world.reviewers.adjudicator),
    ):
        user = User.objects.get(pk=user_id)
        User.objects.filter(pk=user_id).update(
            password=make_password(os.environ["CLINIC_RENEWAL_PASSWORD"])
        )
        key = new_totp_key()
        with (
            runtime_role(),
            tenant_context(user_id, organization),
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "INSERT INTO clinic_app.otp_totp_totpdevice "
                "(name,confirmed,key,step,t0,digits,tolerance,drift,last_t,"
                "user_id,throttling_failure_count,created_at) VALUES "
                "('Synthetic evaluation authenticator',true,%s,"
                "30,0,6,1,0,-1,%s,0,now())",
                [key, user_id],
            )
            cursor.execute(
                "INSERT INTO clinic_app.identity_userpreference "
                "(user_id,theme,density,updated_at) VALUES (%s,%s,'compact',now())",
                [user_id, theme],
            )
        result.update(
            {
                f"{slot}_id": str(user_id),
                f"{slot}_username": user.username,
                f"{slot}_totp_key": key,
            }
        )
    return result


def main() -> int:
    """Use the runner's isolated owner connection and never print clinical data."""
    django.setup()
    organization, clinic, theme = sys.argv[1:]
    result = seed(UUID(organization), UUID(clinic), theme)
    sys.stdout.write(json.dumps(result) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
