#!/usr/bin/env python3
"""Export bge-base-en-v1.5 → OV IR FP32, then NNCF INT8 compress weights.
Saves FP32 to /opt/mnemos/models/bge-base-fp32/ and INT8 to .../bge-base-int8/

Requires: pip install openvino nncf optimum-intel[openvino] psycopg2-binary
"""

import argparse
import os
import shutil
import time
import psycopg2
from transformers import AutoTokenizer
from optimum.intel import OVModelForFeatureExtraction
import nncf
import openvino as ov
from openvino import Core

MODEL_ID = "BAAI/bge-base-en-v1.5"
OUT_FP32 = "/opt/mnemos/models/bge-base-fp32"
OUT_INT8 = "/opt/mnemos/models/bge-base-int8"
DB_DSN = "postgresql://mnemos_user:mnemos_local@192.168.207.67:5432/mnemos"


def fetch_calibration_texts(dsn, n=100):
    conn = psycopg2.connect(dsn)
    cur = conn.cursor()
    cur.execute(
        "SELECT content FROM memories WHERE content IS NOT NULL AND length(content) > 32 "
        "ORDER BY length(content) DESC LIMIT %s",
        (n,),
    )
    rows = [r[0] for r in cur.fetchall()]
    cur.close()
    conn.close()
    print(f"[calib] Fetched {len(rows)} calibration texts (max {len(rows[0]) if rows else 0} chars)")
    return rows


def calibration_fn():
    """Collect calibration data for NNCF; returns a list of tokenized dicts."""
    tokenizer = AutoTokenizer.from_pretrained(MODEL_ID)
    texts = fetch_calibration_texts(DB_DSN)
    result = []
    for i in range(0, len(texts), 4):
        batch = tokenizer(texts[i : i + 4], padding="max_length", truncation=True, max_length=512, return_tensors="np")
        result.append({"input_ids": batch["input_ids"], "attention_mask": batch["attention_mask"]})
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fp32-dir", default=OUT_FP32)
    parser.add_argument("--int8-dir", default=OUT_INT8)
    parser.add_argument("--no-gpu-test", action="store_true")
    args = parser.parse_args()

    os.makedirs(args.fp32_dir, exist_ok=True)
    os.makedirs(args.int8_dir, exist_ok=True)

    # Step 1: Export FP32 IR
    t0 = time.time()
    print("[step1] Exporting FP32 ONNX → OV IR ...")
    model = OVModelForFeatureExtraction.from_pretrained(MODEL_ID, export=True, compile=False, device="cpu")
    model.save_pretrained(args.fp32_dir)
    size_fp32 = sum(os.path.getsize(os.path.join(args.fp32_dir, f)) for f in os.listdir(args.fp32_dir)) / 1e6
    print(f"[step1] → {args.fp32_dir} ({size_fp32:.1f} MB, {time.time()-t0:.1f}s)")

    # Step 2: NNCF compress_weights → INT8 (ASYMMETRIC, group_size=128)
    t1 = time.time()
    print("[step2] NNCFcompress_weights → INT8 (int8_asym, group_size=128)...")
    core = Core()
    xmls = sorted(f for f in os.listdir(args.fp32_dir) if f.endswith(".xml"))
    if not xmls:
        raise FileNotFoundError(f"No IR found in {args.fp32_dir}")

    model_path = os.path.join(args.fp32_dir, xmls[0])
    ov_model = core.read_model(model_path)
    compressed = nncf.compress_weights(ov_model, mode=nncf.CompressWeightsMode.INT8_ASYM, ratio=0.8)
    ov.save_model(compressed, os.path.join(args.int8_dir, xmls[0]))
    # Copy .bin and non-model files
    for f in os.listdir(args.fp32_dir):
        if f.endswith(".bin") or not f.endswith((".xml", ".bin")):
            shutil.copy2(os.path.join(args.fp32_dir, f), os.path.join(args.int8_dir, f))

    size_int8 = sum(os.path.getsize(os.path.join(args.int8_dir, f)) for f in os.listdir(args.int8_dir)) / 1e6
    print(f"[step2] → {args.int8_dir} ({size_int8:.1f} MB, {time.time()-t1:.1f}s)")
    print(f"[done]  Total {time.time()-t0:.1f}s  |  Compression {size_fp32/size_int8:.1f}x")

    if not args.no_gpu_test:
        _gpu_test(args.fp32_dir, "FP32")
        _gpu_test(args.int8_dir, "INT8")


def _gpu_test(ir_dir, label):
    try:
        core = Core()
        xmls = sorted(f for f in os.listdir(ir_dir) if f.endswith(".xml"))
        if not xmls:
            print(f"[gpu]  No IR in {ir_dir}, skipping GPU test")
            return
        m = core.read_model(os.path.join(ir_dir, xmls[0]))
        core.compile_model(m, "GPU")
        print(f"[gpu]  \u2705 {label} compiles on GPU")
    except Exception as e:
        err = str(e)
        tag = "#34856" if "clBuildProgram" in err.lower() else "#33190" if "cisa" in err.lower() else ""
        print(f"[gpu]  \u26a0\ufe0f  {label} GPU compile FAILED {tag} → {e}")


if __name__ == "__main__":
    main()
