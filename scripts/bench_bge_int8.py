#!/usr/bin/env python3
"""Benchmark INT8-quantized bge-base-en-v1.5 OpenVINO IR on GPU (iGPU). Reports rec/s.

Usage: python3 bench_bge_int8.py [--device GPU|CPU] [--ir /path/to/ir] [--samples 20]
"""

import argparse
import os
import time
import numpy as np
from transformers import AutoTokenizer
import openvino as ov

MODEL_ID = "BAAI/bge-base-en-v1.5"
IR_DIR = "/opt/mnemos/models/bge-base-int8"

SAMPLE_TEXTS = [
    "The quick brown fox jumps over the lazy dog.",
    "Machine learning transforms how we process natural language.",
    "Embedding vectors represent semantic meaning in high-dimensional space.",
    "PostgreSQL is a powerful open-source relational database system.",
    "PyTorch and OpenVINO work together for optimized inference on Intel hardware.",
    "Retrieval-augmented generation combines search with language models.",
    "The MNEMOS system stores memories with vector embeddings for semantic search.",
    "Intel integrated GPUs provide hardware acceleration for neural network inference.",
    "Transformers architecture revolutionized natural language processing since 2017.",
    "Quantization reduces model size and latency while preserving accuracy.",
    "Linux containers provide isolated environments for application deployment.",
    "The cosine similarity metric measures distance between embedding vectors.",
    "Batching inference requests improves throughput on GPU accelerators.",
    "Semantic search finds documents based on meaning rather than keywords.",
    "Knowledge graphs organize information as subject-predicate-object triples.",
    "Command-line interfaces remain essential for system administration tasks.",
    "Vector databases index high-dimensional embeddings for similarity search.",
    "Fine-tuning adapts pre-trained models to specific downstream tasks.",
    "The attention mechanism computes weighted relationships between input tokens.",
    "OpenVINO toolkit optimizes deep learning models for Intel platforms.",
]


def mean_pool(token_embeddings, attention_mask):
    """BGE mean-pooling: average token embeddings weighted by attention mask."""
    mask_expanded = np.expand_dims(attention_mask, axis=-1)
    masked = token_embeddings * mask_expanded
    summed = masked.sum(axis=1)
    counts = mask_expanded.sum(axis=1)
    counts = np.maximum(counts, 1e-9)
    return summed / counts


def bench(ir_dir, device, n_samples, batch_size=1):
    tokenizer = AutoTokenizer.from_pretrained(MODEL_ID)
    core = ov.Core()
    xml_files = sorted(f for f in os.listdir(ir_dir) if f.endswith(".xml"))
    if not xml_files:
        raise FileNotFoundError(f"No .xml files in {ir_dir}")
    model_path = os.path.join(ir_dir, xml_files[0])
    print(f"[bench] Loading {model_path} on device={device}...")
    compiled = core.compile_model(model_path, device)
    infer = compiled.create_infer_request()

    # Warmup
    texts = SAMPLE_TEXTS[:batch_size]
    encoded = tokenizer(texts, padding=True, truncation=True, max_length=512, return_tensors="np")
    infer.infer({"input_ids": encoded["input_ids"], "attention_mask": encoded["attention_mask"]})
    _ = infer.get_tensor("last_hidden_state").data

    # Benchmark
    texts = (SAMPLE_TEXTS * ((n_samples // len(SAMPLE_TEXTS)) + 1))[:n_samples]
    tokens_total = 0
    t0 = time.perf_counter()
    for i in range(0, n_samples, batch_size):
        batch = texts[i : i + batch_size]
        encoded = tokenizer(batch, padding=True, truncation=True, max_length=512, return_tensors="np")
        tokens_total += encoded["input_ids"].size
        infer.infer({"input_ids": encoded["input_ids"], "attention_mask": encoded["attention_mask"]})
    elapsed = time.perf_counter() - t0

    rec_s = n_samples / elapsed
    tok_s = tokens_total / elapsed
    print(f"[bench] {n_samples} samples in {elapsed:.2f}s → {rec_s:.1f} rec/s  ({tok_s:.0f} tok/s)")

    # Verify output shape
    encoded = tokenizer(SAMPLE_TEXTS[:1], padding=True, truncation=True, max_length=512, return_tensors="np")
    infer.infer({"input_ids": encoded["input_ids"], "attention_mask": encoded["attention_mask"]})
    hidden = infer.get_tensor("last_hidden_state").data
    pooled = mean_pool(hidden, encoded["attention_mask"])
    print(f"[bench] Embedding shape: {pooled.shape} → {pooled[0,:5].tolist()[:5]}...")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", default="GPU", choices=["GPU", "CPU"])
    parser.add_argument("--ir", default=IR_DIR, help="Path to IR directory")
    parser.add_argument("--samples", type=int, default=20)
    args = parser.parse_args()
    bench(args.ir, args.device, args.samples)
