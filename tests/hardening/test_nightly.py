from pathlib import Path
import os
import subprocess


def test_require_nightly_coverage(tmp_path):
    curl = tmp_path / 'curl'
    curl.write_text('#!/bin/sh\nexit 0\n')
    curl.chmod(0o755)
    env = {**os.environ, 'PATH': str(tmp_path) + ':' + os.environ['PATH'],
           'MNEMOS_KEYS_FILE': str(tmp_path / 'absent'), 'MNEMOS_MCP_TOKEN': 'sample',
           'OPENAI_API_KEY': '', 'ANTHROPIC_API_KEY': '', 'GOOGLE_API_KEY': ''}
    result = subprocess.run(['bash', str(Path(__file__).resolve().parents[2] / 'ops/bridge-tier2-nightly.sh')],
                            env=env, capture_output=True, timeout=10)
    assert result.returncode != 0
    assert b'overall=pass' not in result.stdout
