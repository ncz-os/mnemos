from pathlib import Path
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[2]


def test_validate_wheel_core_constraint(tmp_path):
    wheel = tmp_path / 'sample.whl'
    with zipfile.ZipFile(wheel, 'w') as archive:
        archive.writestr('sample.dist-info/METADATA', 'Name: sample\nVersion: 1\nRequires-Dist: mnemos-core>=9999\n')
    result = subprocess.run([sys.executable, str(ROOT / 'scripts/validate_release_wheels.py'), str(tmp_path)], capture_output=True)
    assert result.returncode == 1
    assert b'core requirement is not satisfied' in result.stderr


def test_validate_wheel_compatible_core(tmp_path):
    wheel = tmp_path / 'sample.whl'
    with zipfile.ZipFile(wheel, 'w') as archive:
        archive.writestr('sample.dist-info/METADATA', 'Name: sample\nVersion: 1\nRequires-Dist: mnemos-core>=1\n')
    result = subprocess.run([sys.executable, str(ROOT / 'scripts/validate_release_wheels.py'), str(tmp_path)], capture_output=True)
    assert result.returncode == 0
