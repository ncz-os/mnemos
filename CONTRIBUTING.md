# Contributing

Thanks for your interest in MNEMOS.

## License

MNEMOS is licensed under the Apache License, Version 2.0. Contributions to
this repository are accepted under the same license and under the Developer
Certificate of Origin (DCO) — see below.

## Developer Certificate of Origin (DCO)

We use the Developer Certificate of Origin 1.1 to track contribution
provenance. By signing off on a commit, you certify that you wrote the code
or otherwise have the right to contribute it under the project's open-source
license. The full DCO text is at <https://developercertificate.org/>:

```
Developer Certificate of Origin
Version 1.1

Copyright (C) 2004, 2006 The Linux Foundation and its contributors.

Everyone is permitted to copy and distribute verbatim copies of this
license document, but changing it is not allowed.


Developer's Certificate of Origin 1.1

By making a contribution to this project, I certify that:

(a) The contribution was created in whole or in part by me and I
    have the right to submit it under the open source license
    indicated in the file; or

(b) The contribution is based upon previous work that, to the best
    of my knowledge, is covered under an appropriate open source
    license and I have the right under that license to submit that
    work with modifications, whether created in whole or in part
    by me, under the same open source license (unless I am
    permitted to submit under a different license), as indicated
    in the file; or

(c) The contribution was provided directly to me by some other
    person who certified (a), (b) or (c) and I have not modified
    it.

(d) I understand and agree that this project and the contribution
    are public and that a record of the contribution (including all
    personal information I submit with it, including my sign-off) is
    maintained indefinitely and may be redistributed consistent with
    this project or the open source license(s) involved.
```

### Signing commits

Every commit must include a `Signed-off-by` trailer attesting to the DCO.
The easiest way is `git commit -s`, which auto-inserts the trailer using
your configured `user.name` and `user.email`:

```
git commit -s -m "your commit message"
```

The trailer looks like:

```
Signed-off-by: Your Name <you@example.com>
```

PRs without DCO sign-off on every commit will be asked to amend
(`git commit --amend -s`) or rebase with sign-off
(`git rebase --signoff origin/master`).

## Development workflow

Development happens on the canonical GitLab project,
<https://gitlab.com/ncz-os/mnemos>. Open merge requests there; the GitHub
mirror exists only to publish container images.

- Use a feature branch for non-trivial changes.
- Keep commits focused and reviewable; split large changes.
- Install from source:

```bash
python -m pip install -e ".[dev,sqlite]"
mnemos install --profile dev
mnemos serve --profile dev
```

- Run the default test suite before opening a PR:

```bash
pytest -q
```

- Run lint before handoff:

```bash
ruff check . --extend-exclude .venv-ci
```

### Backend-gated parity tests

The `tests/test_persistence_parity.py` suite enumerates backend arms
based on which DSN env vars are set. SQLite always runs; the other
arms are skipped cleanly when their env var is absent:

```bash
# PostgreSQL parity arm
export MNEMOS_TEST_DB='postgres://mnemos:<password>@localhost:5432/mnemos'

# Oracle parity arm
export ORACLE_DSN='oracle://MNEMOS:<password>@localhost:1521/ORCLPDB1'

# IBM Db2 parity arm
export DB2_DSN='db2://MNEMOS:<password>@localhost:50000/MNEMOS'

pytest -q tests/test_persistence_parity.py tests/test_oracle_live.py tests/test_db2_live.py
```

The MySQL and MariaDB backends are covered by their own live suites rather
than the parity harness, and read separate env vars:

```bash
export MYSQL_DSN='mysql://mnemos:<password>@localhost:3306/mnemos'
export MARIADB_DSN='mariadb://mnemos:<password>@localhost:3306/mnemos'

pytest -q tests/test_mysql_backend.py tests/test_mariadb_backend.py
```

Without these env vars, the default `pytest -q` run exercises the
SQLite arm and the unit-level surface for the other backends (fake
cursors, SQL translation safety), and skips the live arms.

- For changes touching tenancy, DAG history, triggers, import/export, or
  auth, include focused regression tests and document the expected
  operator behavior for 404 vs 409 vs 403 outcomes.

### Multi-worker development

The `dev` and `edge` profiles are intentionally single-worker SQLite profiles.
For multi-worker work, use the `server` profile with Redis-backed shared state.
Redis is an optional extra, so install it first:

```bash
python -m pip install -e '.[dev,server,redis]'
export MNEMOS_PROFILE=server
export RATE_LIMIT_STORAGE_URI=redis://localhost:6379/1
export MNEMOS_WORKERS=2
mnemos serve --profile server
```

If `MNEMOS_WORKERS > 1` with `RATE_LIMIT_STORAGE_URI=memory://`, startup logs a
warning because circuit-breaker, rate-limit, and concurrency state are only
process-local.

### Multi-backend development

To target one of the enterprise backends for local development, install the
driver extra and set the matching DSN:

```bash
# Oracle Database
python -m pip install -e '.[dev,server,oracle]'
export MNEMOS_DATABASE_DSN='oracle://MNEMOS:<password>@localhost:1521/ORCLPDB1'
mnemos install --profile server
mnemos serve --profile server

# IBM Db2
python -m pip install -e '.[dev,server,db2]'
export MNEMOS_DATABASE_DSN='db2://MNEMOS:<password>@localhost:50000/MNEMOS'
mnemos install --profile server
mnemos serve --profile server

# MySQL or MariaDB (both use the aiomysql driver)
python -m pip install -e '.[dev,server,mysql]'
export MNEMOS_DATABASE_DSN='mariadb://mnemos:<password>@localhost:3306/mnemos'
mnemos install --profile server
mnemos serve --profile server
```

See [docs/INSTALL.md](docs/INSTALL.md) for full driver and DSN guidance, and
[docs/oracle-port-status.md](docs/oracle-port-status.md) for the current
repository-surface coverage on Oracle.

## Guidelines

- Prefer small, reviewable commits.
- Do not commit secrets, `.env` files, logs, backups, or local infrastructure notes.
- Keep public docs generic and portable.
- Add or update tests when behavior changes.

## Reporting issues

Please include:

- what you expected
- what happened
- reproduction steps
- relevant logs or tracebacks
