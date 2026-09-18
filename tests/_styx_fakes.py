"""A fake Google Drive folder for STYX tests.

Models the parts of the API STYX depends on — server-assigned ids, a
server-computed md5, listing, deletion — so the orchestration can be driven
end to end without a credential. Faults are injectable because the failure
paths are the interesting half of this tool.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from mnemos.tools.styx.errors import StyxUploadError
from mnemos.tools.styx.retention import RemoteArtifact


class FakeDriveFolder:
    """In-memory stand-in for :class:`DriveBackupFolder`."""

    def __init__(self):
        self.files: dict[str, dict] = {}
        self.deleted: list[str] = []
        self._next_id = 1
        # Fault injection.
        self.corrupt_md5 = False
        self.truncate_upload = False
        self.drop_md5 = False
        self.fail_upload = False

    def upload(self, path: Path, name: str) -> RemoteArtifact:
        if self.fail_upload:
            raise StyxUploadError("injected upload failure")
        data = Path(path).read_bytes()
        if self.truncate_upload:
            data = b""
        file_id = f"file-{self._next_id}"
        self._next_id += 1
        digest = hashlib.md5(data).hexdigest()  # noqa: S324 - mirrors Drive's md5Checksum
        if self.corrupt_md5:
            digest = "0" * 32
        self.files[file_id] = {
            "id": file_id,
            "name": name,
            "size": len(data),
            "md5": None if self.drop_md5 else digest,
        }
        return self._artifact(file_id)

    def stat(self, file_id: str) -> RemoteArtifact:
        if file_id not in self.files:
            raise StyxUploadError(f"no such file {file_id}")
        return self._artifact(file_id)

    def list_artifacts(self, host_label: str | None = None) -> list[RemoteArtifact]:
        prefix = f"mnemos-{host_label}-" if host_label else "mnemos-"
        return [self._artifact(fid) for fid, row in self.files.items() if row["name"].startswith(prefix)]

    def delete(self, file_id: str) -> None:
        self.files.pop(file_id, None)
        self.deleted.append(file_id)

    def seed(self, name: str, *, size: int = 4096) -> str:
        """Insert a pre-existing artifact (for retention tests)."""
        file_id = f"file-{self._next_id}"
        self._next_id += 1
        self.files[file_id] = {"id": file_id, "name": name, "size": size, "md5": "seed"}
        return file_id

    def _artifact(self, file_id: str) -> RemoteArtifact:
        row = self.files[file_id]
        return RemoteArtifact(file_id=row["id"], name=row["name"], size=row["size"], md5=row["md5"])
