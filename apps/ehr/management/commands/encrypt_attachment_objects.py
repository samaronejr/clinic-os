"""Re-encrypt already stored attachment objects under tenant envelopes.

Owner-side maintenance command for the task-43 protected-object boundary:
every object stored before envelope encryption existed is replaced in place
with its tenant envelope, preserving the recorded plaintext digest. The
command prints a payload-free JSON receipt and exits nonzero when any
object could not be accounted for.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from django.core.management import BaseCommand, CommandError

from apps.ehr.attachment_migration import (
    migrate_attachment_objects,
    receipt_json,
)
from apps.ehr.attachment_storage import AttachmentStorageError
from apps.identity.management.base import (
    LifecycleCommandError,
    assert_owner_database_role,
)

if TYPE_CHECKING:
    from django.core.management.base import CommandParser

_SCOPE_REQUIRED = "--organization-id is required"
_SCOPE_INVALID = "--organization-id must be a UUID"
_MIGRATION_FAILED = "attachment object migration failed"


class Command(BaseCommand):
    """Migrate stored attachment objects to tenant envelopes."""

    help = (
        "Re-encrypt stored clinical attachment objects under each "
        "organization's tenant envelope. Requires the clinic_owner role."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        """Require at least one explicit organization scope."""
        parser.add_argument(
            "--organization-id",
            action="append",
            required=True,
            help="Organization UUID; repeat for each tenant to migrate.",
        )

    def handle(self, *args: object, **options: object) -> None:
        """Run the owner-side object migration and print its receipt."""
        del args
        raw_ids = options.get("organization_id")
        if not isinstance(raw_ids, list) or not raw_ids:
            raise CommandError(_SCOPE_REQUIRED)
        try:
            organization_ids = [UUID(str(item)) for item in raw_ids]
        except (TypeError, ValueError) as error:
            raise CommandError(_SCOPE_INVALID) from error
        try:
            assert_owner_database_role()
            receipt = migrate_attachment_objects(organization_ids)
        except (AttachmentStorageError, LifecycleCommandError) as error:
            raise CommandError(_MIGRATION_FAILED) from error
        self.stdout.write(receipt_json(receipt))
        if receipt.failed:
            raise CommandError(_MIGRATION_FAILED)
