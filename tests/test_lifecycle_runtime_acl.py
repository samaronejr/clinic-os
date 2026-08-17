# pyright: reportImplicitRelativeImport=false

from uuid import uuid4

import pytest
from apps.identity.models import Organization, User
from django.contrib.auth.hashers import make_password
from django.db import DatabaseError, transaction
from django_otp.plugins.otp_totp.models import TOTPDevice

from management_command_support import generated_password
from otp_test_support import runtime_role

pytestmark = pytest.mark.django_db(transaction=True)


def test_runtime_role_cannot_query_users_or_mutate_lifecycle_tables() -> None:
    with runtime_role():
        with pytest.raises(DatabaseError):
            User.objects.count()
        assert Organization.objects.count() == 0
        assert TOTPDevice.objects.count() == 0
        with transaction.atomic(), pytest.raises(DatabaseError):
            User.objects.create(
                id=uuid4(),
                username=f"synthetic-runtime-denied-{uuid4().hex}",
                password=make_password(generated_password()),
            )
