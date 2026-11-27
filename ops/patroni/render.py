"""Render required credentials before starting Patroni."""
import os
import sys
from pathlib import Path
import yaml


def render(source, destination):
    names = ('MNEMOS_DB_PASSWORD', 'MNEMOS_ADMIN_PASSWORD',
             'MNEMOS_REPLICATION_PASSWORD', 'MNEMOS_PATRONI_PASSWORD')
    values = {name: os.environ.get(name, '') for name in names}
    if any(len(value) < 20 or value.lower().endswith('_local')
           or any(word in value.lower() for word in ('placeholder', 'changeme', 'replace-me'))
           for value in values.values()):
        raise ValueError('Required credentials must contain at least 20 characters')
    cfg = yaml.safe_load(Path(source).read_text())
    cfg['bootstrap']['users']['mnemos_user']['password'] = values[names[0]]
    cfg['postgresql']['authentication']['superuser']['password'] = values[names[1]]
    cfg['postgresql']['authentication']['replication']['password'] = values[names[2]]
    cfg['restapi']['authentication'] = {'username': 'admin', 'password': values[names[3]]}
    os.umask(0o077)
    Path(destination).write_text(yaml.safe_dump(cfg))


if __name__ == '__main__':
    render(*sys.argv[1:])
