"""Google Drive transport for STYX.

Service-account OAuth2, because a daily unattended timer cannot complete an
interactive consent flow and a user-OAuth refresh token is one revocation
away from a silently dead backup.

The Drive API object is injected rather than constructed inside the upload
path, so the orchestration logic above is testable against a fake without a
credential. :func:`build_drive_service` is the only place that needs the real
Google client libraries, and it is not imported until it is called.
"""

from __future__ import annotations

import json
from pathlib import Path

from mnemos.tools.styx.errors import StyxConfigError, StyxUploadError
from mnemos.tools.styx.retention import ARTIFACT_SUFFIX, RemoteArtifact

#: Narrowest scope that can create and prune the backup folder's contents.
#: `drive.file` restricts the service account to files it created itself, so
#: a leaked credential cannot read the rest of a shared drive.
DRIVE_SCOPES = ("https://www.googleapis.com/auth/drive.file",)

_UPLOAD_MIME = "application/octet-stream"
_LIST_FIELDS = "nextPageToken, files(id, name, size, md5Checksum)"
_GET_FIELDS = "id, name, size, md5Checksum"


def _load_credentials_document(raw: str) -> dict:
    """Accept either the JSON document itself or a path to it."""
    candidate = raw.strip()
    if candidate.startswith("{"):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as exc:
            raise StyxConfigError("service-account credential is not valid JSON") from exc

    path = Path(candidate)
    if not path.exists():
        raise StyxConfigError(
            f"service-account credential is neither inline JSON nor an existing file: {candidate[:64]!r}"
        )
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise StyxConfigError(f"service-account credential file {path} is not valid JSON") from exc


def build_drive_service(credentials_json: str | None):
    """Build an authenticated Drive v3 client from a service-account credential."""
    if not credentials_json:
        raise StyxConfigError(
            "no Google Drive credential supplied. Set MNEMOS_STYX_GDRIVE_CREDENTIALS_JSON "
            "to the service-account JSON, or to a path containing it."
        )
    document = _load_credentials_document(credentials_json)
    try:
        from google.oauth2 import service_account  # noqa: PLC0415 - optional dependency
        from googleapiclient.discovery import (
            build,  # noqa: PLC0415 - optional dependency
        )
    except ImportError as exc:  # pragma: no cover - exercised by the import-guard test
        raise StyxConfigError(
            "STYX needs google-api-python-client and google-auth to reach Drive. "
            "Reinstall mnemos-core with its base dependencies"
        ) from exc

    credentials = service_account.Credentials.from_service_account_info(document, scopes=list(DRIVE_SCOPES))
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


class DriveBackupFolder:
    """The operations STYX performs against one Drive folder."""

    def __init__(self, service, folder_id: str):
        self._service = service
        self._folder_id = folder_id

    def upload(self, path: Path, name: str) -> RemoteArtifact:
        """Upload ``path`` as ``name``. Returns what Drive says it stored.

        Resumable so a large bundle survives a transient network fault, and
        always a *create* — STYX never updates an existing artifact in place,
        so a failed transfer cannot damage the last good backup.
        """
        try:
            from googleapiclient.http import (
                MediaFileUpload,  # noqa: PLC0415 - optional dependency
            )
        except ImportError as exc:  # pragma: no cover - exercised by the import-guard test
            raise StyxConfigError(
                "STYX needs google-api-python-client to upload. Reinstall mnemos-core with its base dependencies"
            ) from exc

        media = MediaFileUpload(str(path), mimetype=_UPLOAD_MIME, resumable=True)
        try:
            created = (
                self._service.files()
                .create(
                    body={"name": name, "parents": [self._folder_id]},
                    media_body=media,
                    fields=_GET_FIELDS,
                )
                .execute()
            )
        except Exception as exc:
            raise StyxUploadError(f"uploading {name} to Drive failed: {exc}") from exc
        return _to_artifact(created)

    def stat(self, file_id: str) -> RemoteArtifact:
        """Re-read a file's server-side metadata, including Drive's own MD5."""
        try:
            found = self._service.files().get(fileId=file_id, fields=_GET_FIELDS).execute()
        except Exception as exc:
            raise StyxUploadError(f"reading back Drive file {file_id} failed: {exc}") from exc
        return _to_artifact(found)

    def list_artifacts(self, host_label: str | None = None) -> list[RemoteArtifact]:
        """List STYX artifacts in the folder, optionally for one host only.

        Scoping to ``host_label`` matters: several hosts may share one backup
        folder, and a retention pass must never count another host's
        artifacts toward this host's budget — or prune them.
        """
        prefix = f"mnemos-{host_label}-" if host_label else "mnemos-"
        query = f"'{self._folder_id}' in parents and trashed = false and name contains '{prefix}'"
        artifacts: list[RemoteArtifact] = []
        page_token = None
        while True:
            try:
                response = (
                    self._service.files()
                    .list(
                        q=query,
                        fields=_LIST_FIELDS,
                        pageSize=1000,
                        pageToken=page_token,
                    )
                    .execute()
                )
            except Exception as exc:
                raise StyxUploadError(f"listing Drive folder {self._folder_id} failed: {exc}") from exc

            for item in response.get("files", []):
                name = item.get("name", "")
                # `name contains` is a substring match, so re-filter exactly.
                if not name.startswith(prefix) or not name.endswith(ARTIFACT_SUFFIX):
                    continue
                artifacts.append(_to_artifact(item))

            page_token = response.get("nextPageToken")
            if not page_token:
                return artifacts

    def delete(self, file_id: str) -> None:
        try:
            self._service.files().delete(fileId=file_id).execute()
        except Exception as exc:
            raise StyxUploadError(f"deleting Drive file {file_id} failed: {exc}") from exc


def _to_artifact(item: dict) -> RemoteArtifact:
    raw_size = item.get("size")
    try:
        size = int(raw_size) if raw_size is not None else 0
    except (TypeError, ValueError):
        size = 0
    return RemoteArtifact(
        file_id=item.get("id", ""),
        name=item.get("name", ""),
        size=size,
        md5=item.get("md5Checksum"),
    )
