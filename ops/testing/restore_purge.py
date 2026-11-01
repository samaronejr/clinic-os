"""Replay current tombstones before an isolated restored database is exposed."""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from hashlib import sha256
from typing import TYPE_CHECKING, Never, Protocol

from apps.ehr.attachment_storage import AttachmentStorageError
from apps.retention.purge import reapply_object_tombstones
from apps.retention.storage import SyntheticPurgeStorage

from ops.testing.restore_contract import RestoreContractError

if TYPE_CHECKING:
    from pathlib import Path

_PRIVATE_DIRECTORY_MODE = 0o700

CURRENT_TOMBSTONES_SQL = """
SELECT coalesce(jsonb_agg(to_jsonb(p)||jsonb_build_object('objects',(
 SELECT coalesce(jsonb_agg(jsonb_build_object(
  'key',c.storage_key,'location',c.storage_location) ORDER BY c.storage_key),'[]')
 FROM clinic_app.scribe_audiochunk c
 WHERE p.record_class='raw_audio' AND c.session_id=p.record_id
)) ORDER BY p.id),'[]')::text
FROM clinic_app.retention_artifactpurge p
WHERE p.started_at IS NOT NULL AND p.state IN ('purging','purged')
"""


class PurgeRestoreConnection(Protocol):
    """The existing authenticated isolated-container SQL boundary."""

    def sql(self, statement: str) -> str:
        """Execute a fixed statement on the already-claimed restore target."""
        ...


@dataclass(frozen=True, slots=True)
class MediaRestoreBinding:
    """Bind a copied source namespace to one reserved destination directory."""

    source_location: str
    destination: Path
    directory_id: tuple[int, int]


def restore_media_copy(
    *, source: Path, destination: Path, work_dir: Path
) -> MediaRestoreBinding:
    """Copy private synthetic media into the rehearsal's fresh isolated root."""
    try:
        if (
            not source.is_absolute()
            or source.is_symlink()
            or not source.is_dir()
            or source.stat().st_mode & 0o777 != _PRIVATE_DIRECTORY_MODE
            or not work_dir.is_absolute()
            or work_dir.is_symlink()
            or not work_dir.is_dir()
            or work_dir.stat().st_mode & 0o777 != _PRIVATE_DIRECTORY_MODE
            or not destination.is_absolute()
            or destination.is_symlink()
            or destination.name != "media-restored"
            or destination.parent.resolve() != work_dir.resolve()
            or not destination.is_dir()
            or destination.stat().st_mode & 0o777 != _PRIVATE_DIRECTORY_MODE
            or any(destination.iterdir())
            or destination.resolve().is_relative_to(source.resolve())
        ):
            _fail("restore media copy binding is invalid")
        shutil.copytree(source, destination, symlinks=True, dirs_exist_ok=True)
        directory = destination.stat()
        return MediaRestoreBinding(
            source_location=sha256(
                f"filesystem:{source.resolve()}".encode()
            ).hexdigest(),
            destination=destination.resolve(),
            directory_id=(directory.st_dev, directory.st_ino),
        )
    except OSError:
        _fail("restore media copy failed")


def _object_targets(raw: str) -> list[tuple[str, str]]:
    """Validate every target before any restored object is removed."""
    try:
        targets: object = json.loads(raw)
    except json.JSONDecodeError:
        _fail("restored purge target manifest is invalid")
    if not isinstance(targets, list):
        _fail("restored purge target manifest is invalid")
    objects: list[tuple[str, str]] = []
    for row in targets:
        if (
            not isinstance(row, dict)
            or set(row) != {"key", "location"}
            or not isinstance(row["key"], str)
            or re.fullmatch(r"[a-f0-9]{64}", row["key"]) is None
            or not isinstance(row["location"], str)
            or re.fullmatch(r"[a-f0-9]{64}", row["location"]) is None
        ):
            _fail("restored purge target manifest is invalid")
        objects.append((row["key"], row["location"]))
    return objects


def reapply_purge_tombstones(
    target: PurgeRestoreConnection,
    current_tombstones: str,
    media_store: Path | None,
    *,
    media_binding: MediaRestoreBinding | None = None,
) -> int:
    """Override an older backup with the current authenticated source ledger."""
    try:
        parsed: object = json.loads(current_tombstones)
    except json.JSONDecodeError:
        _fail("current purge ledger is invalid")
    if not isinstance(parsed, list):
        _fail("current purge ledger is invalid")
    # Hex encoding keeps SQL construction closed even for malformed source data.
    encoded = current_tombstones.encode("utf-8").hex()
    raw = target.sql(
        "SELECT clinic_app.retention_restore_tombstones("
        "convert_from(decode('" + encoded + "','hex'),'UTF8')::jsonb)"
    )
    objects = _object_targets(raw)
    if not objects:
        return 0
    if media_store is None or not media_store.is_absolute():
        _fail("restore media root is invalid")
    location = sha256(f"filesystem:{media_store.resolve()}".encode()).hexdigest()
    if media_binding is not None:
        try:
            directory = media_store.stat(follow_symlinks=False)
        except OSError:
            _fail("restored media binding is invalid")
        if (
            media_store.resolve() != media_binding.destination
            or (directory.st_dev, directory.st_ino) != media_binding.directory_id
        ):
            _fail("restored media binding is invalid")
        location = media_binding.source_location
    if {namespace for _key, namespace in objects} != {location}:
        _fail("restored purge storage namespace is invalid")
    try:
        return reapply_object_tombstones(
            storage=SyntheticPurgeStorage(media_store),
            object_keys=[key for key, _namespace in objects],
        )
    except AttachmentStorageError:
        _fail("a restored purge target remains")


def _fail(reason: str) -> Never:
    raise RestoreContractError(reason)
