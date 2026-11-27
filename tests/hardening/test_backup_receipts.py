from pathlib import Path
from types import SimpleNamespace
import os
import subprocess
from mnemos.tools.styx.runner import BackupReceipt, _write_receipt

ROOT = Path(__file__).resolve().parents[2]


def test_preserve_styx_receipt(tmp_path):
    work = tmp_path / 'work'
    state = tmp_path / 'state'
    cfg = SimpleNamespace(work_dir=work, receipt_dir=state)
    receipt = BackupReceipt(host_label='sample', started_at='now')
    path = _write_receipt(cfg, receipt)
    assert path == state / 'styx-last-run.json'
    assert path.exists()


def test_validate_required_backup_source(tmp_path):
    target = tmp_path / 'target'
    target.mkdir()
    host = target / 'sample'
    host.mkdir()
    manifest = host / 'last-backup.json'
    manifest.write_text('{"ok": true}')
    config = tmp_path / 'config'
    config.write_text(f'BACKUP_TARGET={target}\nBACKUP_HOSTNAME=sample\nBACKUP_DIRECTORIES={tmp_path / "absent"}\n')
    result = subprocess.run(['bash', str(ROOT / 'mnemos/tools/backup/mnemos-backup-daily.sh')],
                            env={**os.environ, 'MNEMOS_BACKUP_CONFIG': str(config)}, capture_output=True)
    assert result.returncode != 0
    assert manifest.read_text() == '{"ok": true}'


def test_validate_optional_backup_source(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'sample.txt').write_text('sample')
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    rsync = bin_dir / 'rsync'
    rsync.write_text('#!/bin/bash\nmkdir -p "${@: -1}"\ncp "${@: -2:1}/sample.txt" "${@: -1}/sample.txt"\n')
    rsync.chmod(0o755)
    config = tmp_path / 'config'
    target = tmp_path / 'target'
    config.write_text(f'BACKUP_TARGET={target}\nBACKUP_HOSTNAME=sample\nBACKUP_DIRECTORIES={source}\nOPTIONAL_BACKUP_DIRECTORIES={tmp_path / "absent"}\n')
    env = {**os.environ, 'PATH': str(bin_dir) + ':' + os.environ['PATH'], 'MNEMOS_BACKUP_CONFIG': str(config)}
    script = str(ROOT / 'mnemos/tools/backup/mnemos-backup-daily.sh')
    result = subprocess.run(['bash', script], env=env, capture_output=True)
    assert result.returncode == 0
    manifest = target / 'sample/last-backup.json'
    previous = manifest.read_text()
    config.write_text(config.read_text().replace(f'BACKUP_DIRECTORIES={source}\n', f'BACKUP_DIRECTORIES={source}:{tmp_path / "required"}\n'))
    result = subprocess.run(['bash', script], env=env, capture_output=True)
    assert result.returncode != 0
    assert manifest.read_text() == previous
