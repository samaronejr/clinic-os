"""Append-only audit ledger state model."""

from typing import TYPE_CHECKING, ClassVar, Final
from uuid import UUID

from django.db import models
from django.db.backends.base.base import BaseDatabaseWrapper
from django.db.models import Index

SYSTEM_ORG_ID: Final = UUID("00000000-0000-0000-0000-000000000000")


if TYPE_CHECKING:

    class _InetAddressFieldBase(
        models.GenericIPAddressField[str | int | None, str | None]
    ):
        pass

else:

    class _InetAddressFieldBase(models.GenericIPAddressField):
        pass


class InetAddressField(_InetAddressFieldBase):
    """Store a canonical host address in PostgreSQL's native inet type."""

    def db_type(self, connection: BaseDatabaseWrapper) -> str:  # noqa: ARG002
        """Return the PostgreSQL-native column type."""
        return "inet"


class AuditEvent(models.Model):
    """One immutable semantic event in an organization hash chain."""

    seq = models.BigAutoField(primary_key=True)
    organization_id = models.UUIDField()
    actor_user_id = models.UUIDField(null=True, blank=True)
    event_type = models.CharField(max_length=128)
    component_id = models.CharField(max_length=255)
    component_ip = InetAddressField(null=True, blank=True)
    affected_record_type = models.CharField(  # noqa: DJ001
        max_length=128,
        null=True,
        blank=True,
    )
    affected_record_id = models.CharField(  # noqa: DJ001
        max_length=255,
        null=True,
        blank=True,
    )
    occurred_at_utc = models.DateTimeField()
    payload = models.JSONField()
    prev_hash = models.BinaryField(max_length=32)
    curr_hash = models.BinaryField(max_length=32, unique=True)

    class Meta:
        """Pin the ledger relation and chain-tip lookup index."""

        db_table = "audit_event"
        indexes: ClassVar[list[Index]] = [
            models.Index(
                fields=("organization_id", "-seq"),
                include=("curr_hash",),
                name="audit_event_org_seq_hash_idx",
            )
        ]

    def __str__(self) -> str:
        """Return stable chain and event identifiers."""
        return f"{self.organization_id}:{self.seq}:{self.event_type}"
