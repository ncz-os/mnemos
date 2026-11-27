"""Work directory validation and creation boundaries."""

import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from mnemos.tools.styx import runner
from mnemos.tools.styx.errors import StyxIntegrityError


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", ["", "new/work"])
async def test_symlink_rejected_before_creation(tmp_path, suffix, monkeypatch):
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    calls = []
    original = Path.mkdir

    def record_mkdir(path, *args, **kwargs):
        calls.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", record_mkdir)
    config = SimpleNamespace(host_label="test", dry_run=True, work_dir=link / suffix)
    with pytest.raises(StyxIntegrityError, match="symlinks"):
        await runner.run_backup(config)
    assert calls == []
    assert list(target.iterdir()) == []


@pytest.mark.skipif(getattr(os, "geteuid", lambda: 1)() == 0, reason="ownership model assumes a non-root caller")
def test_system_symlink_above_owned_ancestor(tmp_path, monkeypatch):
    system = tmp_path / "private-var"
    system.mkdir(mode=0o755)
    owned = system / "user-temp"
    owned.mkdir(mode=0o700)
    alias = tmp_path / "var"
    alias.symlink_to(system, target_is_directory=True)
    original = os.lstat
    system_paths = {tmp_path, *tmp_path.parents, alias, system}

    def system_lstat(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if Path(path) in system_paths:
            values = list(info)
            values[4] = 0
            return os.stat_result(values)
        return info

    monkeypatch.setattr(os, "lstat", system_lstat)
    work = alias / "user-temp" / "new" / "work"
    runner._prepare_work_dir(work)
    assert (owned / "new" / "work").is_dir()
    assert stat.S_IMODE(original(work).st_mode) == 0o700


def test_created_components_are_private_and_owned(tmp_path):
    work = tmp_path / "new" / "work"
    runner._prepare_work_dir(work)
    for directory in (work.parent, work):
        info = os.lstat(directory)
        assert stat.S_ISDIR(info.st_mode)
        assert info.st_uid == os.getuid()
        assert stat.S_IMODE(info.st_mode) == 0o700


@pytest.mark.parametrize("mode", [0o755, 0o750, 0o600, 0o500, 0o1700])
def test_existing_work_dir_requires_exact_mode(tmp_path, mode):
    work = tmp_path / "work"
    work.mkdir()
    work.chmod(mode)
    try:
        with pytest.raises(StyxIntegrityError, match="owned and private"):
            runner._prepare_work_dir(work)
    finally:
        work.chmod(0o700)


@pytest.mark.parametrize("field,value", [(4, os.getuid() + 1), (0, stat.S_IFREG | 0o700)])
def test_created_directory_is_reverified(tmp_path, monkeypatch, field, value):
    work = tmp_path / "work"
    original = os.lstat

    def changed_lstat(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if Path(path) == work:
            values = list(info)
            values[field] = value
            return os.stat_result(values)
        return info

    monkeypatch.setattr(os, "lstat", changed_lstat)
    with pytest.raises(StyxIntegrityError, match="owned and private"):
        runner._prepare_work_dir(work)


def test_owned_ancestor_rejects_nested_redirect(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    (target / "existing").mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(StyxIntegrityError, match="symlinks"):
        runner._prepare_work_dir(link / "existing" / "new" / "work")
    assert not (target / "existing" / "new").exists()
