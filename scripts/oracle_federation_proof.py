"""Federation-HA proof: PYTHIA-Postgres → PROTEUS-Oracle pull cycle.

Emits a signed JSON artifact under docs/proof/ that captures:
- Peer registration on the Oracle side
- Sync trigger result (pulled / new / updated counts)
- Federated row count delta (federation_source IS NOT NULL)
- A sample federated memory row to prove `federation_source` tagging
- HMAC-SHA256 over the evidence body

Reads the current peer state via the Oracle backend rather than
re-triggering a sync — running this script multiple times will not
double-pull.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import hashlib
import hmac
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

ORACLE_DSN = os.environ.get("ORACLE_PROOF_DSN", "oracle://mnemos:mnemos_dev@192.168.207.25:1521/FREEPDB1")
HMAC_KEY = os.environ.get("ORACLE_PROOF_HMAC_KEY", "mnemos-oracle-proof-v1")


def _git_head() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True).strip()
    except Exception:
        return "unknown"


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


async def _run() -> dict[str, Any]:
    from mnemos.persistence.oracle import create_oracle_pool

    pool = await create_oracle_pool(ORACLE_DSN, min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            cur = conn.cursor()
            await cur.execute("SELECT BANNER_FULL FROM v$version")
            (banner,) = await cur.fetchone()
            await cur.execute(
                "SELECT id, name, base_url, enabled, sync_interval_secs, "
                "last_sync_at, last_sync_cursor, total_pulled, compat_mode "
                "FROM federation_peers"
            )
            cols = [c[0].lower() for c in cur.description]
            peers = [dict(zip(cols, row)) for row in await cur.fetchall()]

            await cur.execute("SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL")
            (total_live,) = await cur.fetchone()
            await cur.execute(
                "SELECT COUNT(*) FROM memories WHERE federation_source IS NOT NULL AND deleted_at IS NULL"
            )
            (federated_count,) = await cur.fetchone()
            await cur.execute(
                "SELECT federation_source, COUNT(*) FROM memories "
                "WHERE federation_source IS NOT NULL AND deleted_at IS NULL "
                "GROUP BY federation_source"
            )
            by_source = {row[0]: row[1] for row in await cur.fetchall()}

            await cur.execute(
                "SELECT id, category, owner_id, namespace, "
                "federation_source, federation_remote_updated "
                "FROM memories "
                "WHERE federation_source IS NOT NULL AND deleted_at IS NULL "
                "FETCH FIRST 3 ROWS ONLY"
            )
            sample_cols = [c[0].lower() for c in cur.description]
            sample_rows = [dict(zip(sample_cols, row)) for row in await cur.fetchall()]

            await cur.execute(
                "SELECT id, peer_id, started_at, finished_at, memories_pulled, "
                "memories_new, memories_updated, error, cursor_after "
                "FROM federation_sync_log "
                "ORDER BY started_at DESC FETCH FIRST 5 ROWS ONLY"
            )
            log_cols = [c[0].lower() for c in cur.description]
            log_rows = [dict(zip(log_cols, row)) for row in await cur.fetchall()]
            cur.close()
    finally:
        await pool.close()

    body = {
        "schema": "mnemos-oracle-federation-proof/v1",
        "run_utc": _now(),
        "git_head_sha": _git_head(),
        "oracle_target": ORACLE_DSN.split("@")[-1],
        "oracle_version": banner.splitlines()[0],
        "peers": peers,
        "live_memory_total": total_live,
        "federated_memory_count": federated_count,
        "federated_by_source": by_source,
        "sample_federated_rows": sample_rows,
        "recent_sync_log": log_rows,
    }
    body_json = json.dumps(body, separators=(",", ":"), sort_keys=True, default=str)
    sig = hmac.new(HMAC_KEY.encode(), body_json.encode(), hashlib.sha256).hexdigest()
    return {
        "evidence": body,
        "hmac_sha256": sig,
        "hmac_key_id": hashlib.sha256(HMAC_KEY.encode()).hexdigest()[:16],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--out",
        default=str(
            REPO_ROOT
            / "docs"
            / "proof"
            / f"oracle-federation-{_dt.datetime.now(_dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
        ),
    )
    args = ap.parse_args()
    artifact = asyncio.run(_run())
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(artifact, indent=2, default=str))
    ev = artifact["evidence"]
    print(
        f"wrote {out}\n"
        f"  oracle:    {ev['oracle_version']}\n"
        f"  total:     {ev['live_memory_total']}\n"
        f"  federated: {ev['federated_memory_count']}\n"
        f"  by_source: {ev['federated_by_source']}\n"
        f"  peers:     {len(ev['peers'])}\n"
        f"  hmac:      {artifact['hmac_sha256'][:16]}…"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
