#!/usr/bin/env python3
"""Compare native vs pure-Python federation feed JSON serialization."""

from __future__ import annotations

import argparse
import json
import random
import string
import sys
import time
from datetime import datetime, timedelta, timezone
from importlib import util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
try:
    from mnemos.domain.federation import native_bridge
except ModuleNotFoundError:
    spec = util.spec_from_file_location(
        "mnemos_federation_native_bridge",
        REPO_ROOT / "mnemos" / "domain" / "federation" / "native_bridge.py",
    )
    if spec is None or spec.loader is None:
        raise
    native_bridge = util.module_from_spec(spec)
    spec.loader.exec_module(native_bridge)


def _content(rng: random.Random, size: int) -> str:
    alphabet = string.ascii_letters + string.digits + " .,;:-_/"
    return "".join(rng.choice(alphabet) for _ in range(size))


def _rows(count: int, content_bytes: int) -> list[dict]:
    rng = random.Random(1337)
    start = datetime(2026, 5, 28, 12, 0, tzinfo=timezone.utc)
    rows = []
    for idx in range(count):
        updated = start + timedelta(seconds=idx)
        rows.append(
            {
                "id": f"mem_{idx:08d}",
                "content": _content(rng, content_bytes),
                "category": "federation",
                "subcategory": None,
                "metadata": {"source": "bench", "idx": idx},
                "quality_rating": 75,
                "verbatim_content": None,
                "owner_id": "owner-1",
                "group_id": None,
                "namespace": "default",
                "permission_mode": 644,
                "source_model": None,
                "source_provider": None,
                "source_session": None,
                "source_agent": "bench",
                "created": updated,
                "updated": updated,
                "archived_at": None,
            }
        )
    return rows


def _time_call(label: str, func, rows: list[dict], rounds: int) -> tuple[str, float, int]:
    size = 0
    start = time.perf_counter()
    for _ in range(rounds):
        payload = func(rows)
        size = len(payload)
    elapsed = time.perf_counter() - start
    return label, elapsed, size


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=10_000)
    parser.add_argument("--content-bytes", type=int, default=512)
    parser.add_argument("--rounds", type=int, default=5)
    args = parser.parse_args()

    rows = _rows(args.rows, args.content_bytes)
    py_label, py_elapsed, py_size = _time_call(
        "python",
        native_bridge.pure_python_serialize_memory_rows,
        rows,
        args.rounds,
    )
    print(f"{py_label}: {py_elapsed:.4f}s payload={py_size:,} bytes")

    if not native_bridge.NATIVE_AVAILABLE:
        print("native: unavailable; run `cd mnemos-rust-ext && maturin develop --release`")
        return

    native_label, native_elapsed, native_size = _time_call(
        "native",
        native_bridge.serialize_memory_rows,
        rows,
        args.rounds,
    )
    print(f"{native_label}: {native_elapsed:.4f}s payload={native_size:,} bytes")
    if native_elapsed > 0.0:
        print(f"speedup: {py_elapsed / native_elapsed:.2f}x")

    if json.loads(native_bridge.serialize_memory_rows(rows[:10])) != json.loads(
        native_bridge.pure_python_serialize_memory_rows(rows[:10])
    ):
        raise SystemExit("native output does not match python output")


if __name__ == "__main__":
    main()
