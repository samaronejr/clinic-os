"""The only runnable purge transport is the local synthetic storage fake."""

from apps.ehr.attachment_storage import (
    AttachmentStorageError,
    FilesystemAttachmentStorage,
)


class SyntheticPurgeStorage(FilesystemAttachmentStorage):
    """Keep deletion and its independent absence observation on the same root."""

    def absent(self, key: str) -> bool:
        """Uncertain filesystem status, symlinks and pending markers fail closed."""
        try:
            for path in (self._path(key), self._receipt_path(key)):
                try:
                    path.lstat()
                except FileNotFoundError:
                    continue
                return False
        except OSError as error:
            raise AttachmentStorageError from error
        return True
