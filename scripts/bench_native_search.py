#!/usr/bin/env python3
"""Compare native vs pure-Python cosine similarity helpers."""

from __future__ import annotations

import argparse
import math
import random
import time

from mnemos.domain.search import native_bridge

try:
    import numpy as np
except ImportError:  # pragma: no cover - optional benchmark mode
    np = None


def _vectors(rows: int, dims: int) -> tuple[list[float], list[list[float]]]:
    rng = random.Random(1337)
    query = [math.sin(idx + 1) for idx in range(dims)]
    corpus = [[rng.uniform(-1.0, 1.0) for _ in range(dims)] for _ in range(rows)]
    return query, corpus


def _time_call(label: str, func, query: list[float], corpus: list[list[float]], rounds: int) -> tuple[str, float]:
    start = time.perf_counter()
    for _ in range(rounds):
        func(query, corpus)
    elapsed = time.perf_counter() - start
    return label, elapsed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=10_000)
    parser.add_argument("--dims", type=int, default=384)
    parser.add_argument("--rounds", type=int, default=3)
    args = parser.parse_args()

    query, corpus = _vectors(args.rows, args.dims)
    python_label, python_elapsed = _time_call(
        "python",
        native_bridge.pure_python_batch_cosine_similarity,
        query,
        corpus,
        args.rounds,
    )
    print(f"{python_label}: {python_elapsed:.4f}s")

    if not native_bridge.NATIVE_AVAILABLE:
        print("native: unavailable; run `cd mnemos-rust-ext && maturin develop`")
        return

    native_label, native_elapsed = _time_call(
        "native-list",
        native_bridge.batch_cosine_similarity,
        query,
        corpus,
        args.rounds,
    )
    print(f"{native_label}: {native_elapsed:.4f}s")
    if native_elapsed > 0.0:
        print(f"list speedup: {python_elapsed / native_elapsed:.2f}x")

    if np is None:
        return

    query_np = np.asarray(query, dtype=np.float32)
    corpus_np = np.asarray(corpus, dtype=np.float32)
    native_np_label, native_np_elapsed = _time_call(
        "native-numpy",
        native_bridge.batch_cosine_similarity,
        query_np,
        corpus_np,
        args.rounds,
    )
    print(f"{native_np_label}: {native_np_elapsed:.4f}s")
    if native_np_elapsed > 0.0:
        print(f"numpy speedup: {python_elapsed / native_np_elapsed:.2f}x")


if __name__ == "__main__":
    main()
