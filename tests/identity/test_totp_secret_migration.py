"""0048 moves every existing TOTP seed intact, and its reverse restores them.

The device table is FORCE RLS and clinic_owner has no policy that shows every
device, so a data move run as the migration role would silently see no rows,
leave every seed readable in ``key`` and strand every authenticator. These tests
seed devices the way django-otp wrote them before 0048, then migrate across it.
Seeds are compared as flags so a failure never prints one.
"""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.identity.models import User
from django.db import DatabaseError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django_otp.plugins.otp_totp.models import TOTPDevice

from otp_test_support import current_user_guc, runtime_role

if TYPE_CHECKING:
    from uuid import UUID

pytestmark = pytest.mark.django_db(transaction=True)

BEFORE_BOUNDARY = [("identity", "0047_insurance_approval_permission")]


def _migrate_head() -> None:
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())


def _legacy_device(user_id: UUID, key: str) -> int:
    """Write a device as the pre-0048 runtime did, seed in ``key``."""
    with runtime_role(), current_user_guc(user_id):
        device = TOTPDevice.objects.create(
            user_id=user_id, name="Clinic OS authenticator", confirmed=True, key=key
        )
    return int(device.pk)


def _key(device_id: int, user_id: UUID) -> str:
    with runtime_role(), current_user_guc(user_id):
        return str(TOTPDevice.objects.values_list("key", flat=True).get(pk=device_id))


def _stored(device_id: int) -> bytes | None:
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET LOCAL ROLE clinic_resolver")
        cursor.execute(
            "SELECT secret FROM clinic_app.identity_totpsecret WHERE device_id = %s",
            [device_id],
        )
        row = cursor.fetchone()
    return bytes(row[0]) if row is not None else None


def test_existing_seeds_move_into_the_store_and_the_reverse_restores_them() -> None:
    user = User.objects.create(username=f"synthetic-seed-{uuid4().hex}")
    seed = secrets.token_hex(20)
    try:
        MigrationExecutor(connection).migrate(BEFORE_BOUNDARY)
        # Given a device written before 0048, its seed in upper-case hex.
        device_id = _legacy_device(user.pk, seed.upper())
        # When 0048 runs forward, the seed moves intact and key is emptied.
        _migrate_head()
        moved = _stored(device_id) == bytes.fromhex(seed)
        cleared = _key(device_id, user.pk) == ""
        assert moved, "the existing seed did not reach the resolver-held store"
        assert cleared, "the existing seed is still readable in key"
        # Then the reverse writes it back, so Python verification keeps working.
        MigrationExecutor(connection).migrate(BEFORE_BOUNDARY)
        restored = _key(device_id, user.pk) == seed
        assert restored, "the reverse did not restore the seed into key"
    finally:
        _migrate_head()
    moved_again = _stored(device_id) == bytes.fromhex(seed)
    assert moved_again, "re-applying 0048 did not move the restored seed"


def test_a_seed_that_cannot_move_intact_stops_the_migration() -> None:
    user = User.objects.create(username=f"synthetic-seed-{uuid4().hex}")
    MigrationExecutor(connection).migrate(BEFORE_BOUNDARY)
    try:
        intact = _legacy_device(user.pk, secrets.token_hex(20))
        _legacy_device(user.pk, "not-a-hex-seed")
        # When 0048 meets a seed it cannot decode, nothing moves at all.
        with pytest.raises(DatabaseError, match="cannot move intact"):
            _migrate_head()
        untouched = _key(intact, user.pk) != ""
        assert untouched, "a refused migration still cleared an intact seed"
    finally:
        # The refusal left the database before 0048; drop the undecodable seed
        # so the boundary is re-applied for every later test.
        with runtime_role(), current_user_guc(user.pk):
            TOTPDevice.objects.filter(user_id=user.pk, key="not-a-hex-seed").delete()
        _migrate_head()
    cleared = _key(intact, user.pk) == ""
    assert cleared, "the intact seed is still readable in key"
