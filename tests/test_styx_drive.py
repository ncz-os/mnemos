"""STYX Drive transport — credential handling and listing behaviour."""

from __future__ import annotations

import json

import pytest

from mnemos.tools.styx.drive import (
    DRIVE_SCOPES,
    DriveBackupFolder,
    _load_credentials_document,
)
from mnemos.tools.styx.errors import StyxConfigError, StyxUploadError


class _Executable:
    def __init__(self, result):
        self._result = result

    def execute(self):
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class _Files:
    def __init__(self, pages):
        self._pages = list(pages)
        self.queries: list[str] = []
        self.deleted: list[str] = []

    def list(self, *, q, fields, pageSize, pageToken):  # noqa: N803 - Google's spelling
        self.queries.append(q)
        return _Executable(self._pages.pop(0))

    def delete(self, *, fileId):  # noqa: N803 - Google's spelling
        self.deleted.append(fileId)
        return _Executable({})


class _Service:
    def __init__(self, files):
        self._files = files

    def files(self):
        return self._files


def test_scope_is_the_narrow_one():
    """drive.file confines a leaked credential to files STYX itself created."""
    assert DRIVE_SCOPES == ("https://www.googleapis.com/auth/drive.file",)


def test_credentials_accept_inline_json():
    doc = _load_credentials_document(json.dumps({"type": "service_account"}))
    assert doc["type"] == "service_account"


def test_credentials_accept_a_file_path(tmp_path):
    path = tmp_path / "sa.json"
    path.write_text(json.dumps({"type": "service_account", "project_id": "p"}))
    assert _load_credentials_document(str(path))["project_id"] == "p"


def test_credentials_reject_garbage():
    with pytest.raises(StyxConfigError, match="neither inline JSON nor an existing file"):
        _load_credentials_document("/no/such/credential.json")


def test_list_artifacts_paginates_and_filters_exactly():
    """`name contains` is a substring match, so results are re-filtered.

    Without the exact re-filter a file merely *mentioning* the prefix could
    be counted as a backup — and then pruned as one.
    """
    pages = [
        {
            "files": [
                {"id": "1", "name": "mnemos-pythia-20260917T024000Z.mif.tar.gz.age", "size": "10"},
                {"id": "2", "name": "notes-about-mnemos-pythia-.txt", "size": "5"},
            ],
            "nextPageToken": "tok",
        },
        {
            "files": [
                {
                    "id": "3",
                    "name": "mnemos-pythia-20260916T024000Z.mif.tar.gz.age",
                    "size": "11",
                    "md5Checksum": "abc",
                },
                {"id": "4", "name": "mnemos-otherhost-20260916T024000Z.mif.tar.gz.age", "size": "9"},
            ]
        },
    ]
    files = _Files(pages)
    folder = DriveBackupFolder(_Service(files), "folder-1")

    found = folder.list_artifacts("pythia")

    assert [a.file_id for a in found] == ["1", "3"]
    assert found[1].md5 == "abc"
    assert found[0].size == 10
    assert "'folder-1' in parents" in files.queries[0]
    assert "trashed = false" in files.queries[0]


def test_list_artifacts_surfaces_api_errors():
    files = _Files([RuntimeError("quota exceeded")])
    folder = DriveBackupFolder(_Service(files), "folder-1")
    with pytest.raises(StyxUploadError, match="listing Drive folder"):
        folder.list_artifacts("pythia")


def test_delete_surfaces_api_errors():
    class _Boom(_Files):
        def delete(self, *, fileId):  # noqa: N803
            return _Executable(RuntimeError("permission denied"))

    folder = DriveBackupFolder(_Service(_Boom([])), "folder-1")
    with pytest.raises(StyxUploadError, match="deleting Drive file"):
        folder.delete("x")
