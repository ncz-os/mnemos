from pathlib import Path
import subprocess
import sys
import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_validate_ha_tls_configuration():
    compose = yaml.safe_load((ROOT / 'ops/patroni/docker-compose.yml').read_text())
    command = compose['services']['etcd3']['command']
    assert '--client-cert-auth=true' in command
    assert '--peer-client-cert-auth=true' in command
    for node in ('pythia', 'cerberus'):
        cfg = yaml.safe_load((ROOT / f'ops/patroni/patroni-{node}.yml').read_text())
        assert cfg['restapi']['verify_client'] == 'required'
        assert cfg['etcd3']['protocol'] == 'https'


def test_validate_ha_required_credentials(tmp_path):
    result = subprocess.run([sys.executable, str(ROOT / 'ops/patroni/render.py'),
                             str(ROOT / 'ops/patroni/patroni-pythia.yml'), str(tmp_path / 'config.yml')],
                            env={}, capture_output=True)
    assert result.returncode != 0
    assert not (tmp_path / 'config.yml').exists()


def test_validate_ha_runtime_role(tmp_path):
    result = subprocess.run([sys.executable, str(ROOT / 'ops/patroni/render.py'),
                             str(ROOT / 'ops/patroni/patroni-pythia.yml'), str(tmp_path / 'config.yml')],
                            env={k: 'sample-value-9876543210' for k in
                                 ('MNEMOS_DB_PASSWORD', 'MNEMOS_ADMIN_PASSWORD', 'MNEMOS_REPLICATION_PASSWORD', 'MNEMOS_PATRONI_PASSWORD')},
                            capture_output=True)
    assert result.returncode == 0
    cfg = yaml.safe_load((tmp_path / 'config.yml').read_text())
    assert cfg['bootstrap']['users']['mnemos_user']['options'] == ['nocreaterole', 'nocreatedb', 'nosuperuser']
