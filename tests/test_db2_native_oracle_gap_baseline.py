"""DB2-native Oracle-fall-through gap baseline (Slice 0 — the "poison-pill" exerciser).

GRAEAE-blessed severance plan (mem_1781315914455_0db361): the native dialect
must have ZERO Oracle-SQL fall-through. The native cursor already raises on
Oracle-isms (``:name`` binds, SYSTIMESTAMP, FROM DUAL, ROWNUM, ...), but the
8 latent gap areas pass today only because those methods are never *exercised*
in native mode. This walker forces every tx-only public async repository method
to execute against the live DB2 12.1.5 EAP under MNEMOS_DB2_DIALECT=native and
classifies each as native-OK or an Oracle fall-through (guard trip).

Run:  DB2_DSN=db2://... .venv/bin/python -m pytest -s tests/test_db2_native_oracle_gap_baseline.py
Skipped unless DB2_DSN is set.
"""

from __future__ import annotations

import asyncio
import inspect
import os
from types import SimpleNamespace

import pytest

pytest.importorskip("ibm_db", reason="ibm_db driver not installed")

DB2_DSN = os.environ.get("DB2_DSN")
pytestmark = pytest.mark.skipif(not DB2_DSN, reason="DB2_DSN not set; live EAP probe skipped")

_GUARD_MARK = "native cursor received"

# Repo backend attributes wired in Db2Backend.__init__.
_REPO_ATTRS = [
    "_memories_repo", "_kg_triples_repo", "_memory_versions_repo",
    "_memory_branches_repo", "_compression_repo", "_webhooks_repo",
    "_consultations_audit_repo", "_federation_repo", "_state_kv_repo",
    "_oauth_repo", "_sessions_repo", "_consultations_repo", "_audit_chain_repo",
]


async def _build_native_backend():
    from mnemos.persistence import db2 as m
    settings = SimpleNamespace(database=SimpleNamespace(embedding_dim=768, db2_dialect="native"))
    pool = await m.create_db2_native_pool(DB2_DSN, min_size=1, max_size=2)
    be = m.Db2BackendNative(pool, settings)
    try:
        await be.open()
    except Exception:
        pass  # open probe failures are not the subject of this test
    return m, be


def _tx_only_methods(repo):
    """Public async methods whose call signature is exactly (tx) beyond self."""
    out = []
    for name, fn in inspect.getmembers(repo, predicate=inspect.iscoroutinefunction):
        if name.startswith("_"):
            continue
        params = [p for p in inspect.signature(fn).parameters.values()
                  if p.name != "self"]
        # exactly one required positional param (tx), no other required params
        required = [p for p in params
                    if p.default is inspect.Parameter.empty
                    and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
        has_varkw = any(p.kind is p.VAR_KEYWORD for p in params)
        if len(required) == 1 and required[0].name in ("tx",) and not has_varkw:
            out.append((name, fn))
    return out


@pytest.mark.asyncio
async def test_native_oracle_gap_baseline():
    m, be = await _build_native_backend()
    oracle_gaps, native_ok, inconclusive = [], [], []
    try:
        for attr in _REPO_ATTRS:
            repo = getattr(be, attr, None)
            if repo is None:
                continue
            for name, _fn in _tx_only_methods(repo):
                label = f"{attr}.{name}"
                try:
                    async with be.transactional() as tx:
                        await getattr(repo, name)(tx)
                    native_ok.append(label)
                except RuntimeError as e:
                    if _GUARD_MARK in str(e):
                        oracle_gaps.append((label, str(e).split(" — ")[0]))
                    else:
                        inconclusive.append((label, repr(e)[:80]))
                except Exception as e:
                    # data/FK errors etc. — SQL was DB2-native (guard did not trip)
                    inconclusive.append((label, type(e).__name__ + ": " + str(e).splitlines()[0][:80]))

        # ping() — no tx; guard trip is swallowed by ping try/except -> latent False
        ping_latent = False
        try:
            r = await be.ping()
            ping_latent = (r is False)
        except RuntimeError as e:
            if _GUARD_MARK in str(e):
                oracle_gaps.append(("backend.ping", "Oracle DUAL table"))
    finally:
        try:
            await be.close()
        except Exception:
            pass

    print("\n==== DB2-NATIVE ORACLE FALL-THROUGH BASELINE ====")
    print(f"Oracle-SQL gaps (guard tripped): {len(oracle_gaps)}")
    for lbl, reason in sorted(oracle_gaps):
        print(f"  GAP   {lbl:55s} {reason}")
    if ping_latent:
        print(f"  GAP   {'backend.ping':55s} Oracle DUAL (swallowed -> returns False)")
    print(f"native-OK tx-only methods: {len(native_ok)}")
    print(f"inconclusive (non-guard error, SQL was native): {len(inconclusive)}")
    for lbl, why in sorted(inconclusive):
        print(f"  ?     {lbl:55s} {why}")

    # Regression invariant (post-slice-1): NO tx-only repo method may emit
    # Oracle SQL in native mode. As fall-through slices land, this stays green
    # and fences regressions. (Keyword-only methods need kwarg synthesis — a
    # later iteration extends coverage beyond tx-only signatures.)
    gap_labels = [g[0] for g in oracle_gaps] + (["backend.ping"] if ping_latent else [])
    assert not gap_labels, f"Oracle fall-through detected in native mode: {gap_labels}"
