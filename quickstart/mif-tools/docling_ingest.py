#!/usr/bin/env python3
"""Documents -> IBM Docling -> mnemos (Db2-backed agent memory).

Converts PDF/DOCX/PPTX/HTML/MD via Docling to clean Markdown, splits on headings
into recall-sized chunks (section context preserved), and either POSTs them to
mnemos `/v1/memories/bulk` or emits a portable MIF bundle for `mnemos mif import`.

    pip install docling requests
    python docling_ingest.py <file-or-dir> [--mnemos-url URL] [--category reference]
        [--source-tag NAME] [--emit-mif OUT.jsonld] [--max-chars 1800]

Design: Docling solves document->structure; mnemos+Db2 solve durable vector recall;
MIF (--emit-mif) is the vendor-neutral hand-off between them.
"""
from __future__ import annotations
import argparse
import json
import os
import re
import sys
from pathlib import Path

DOC_EXTS = {".pdf", ".docx", ".pptx", ".html", ".htm", ".md", ".txt"}


def to_markdown(path: Path) -> str:
    """Docling: document -> Markdown (layout/tables/headings preserved)."""
    from docling.document_converter import DocumentConverter

    return DocumentConverter().convert(str(path)).document.export_to_markdown()


def chunk_by_headings(md: str, max_chars: int) -> list[str]:
    """Split on Markdown headings; keep the nearest heading as context in each
    chunk; hard-wrap oversized sections on paragraph boundaries."""
    parts, cur, heading = [], [], ""
    for line in md.splitlines():
        if re.match(r"^#{1,6}\s", line):
            if cur:
                parts.append("\n".join(cur).strip())
            heading, cur = line, [line]
        else:
            cur.append(line)
    if cur:
        parts.append("\n".join(cur).strip())

    out: list[str] = []
    for p in filter(None, parts):
        if len(p) <= max_chars:
            out.append(p)
            continue
        buf = ""
        for para in p.split("\n\n"):
            if buf and len(buf) + len(para) > max_chars:
                out.append(buf.strip())
                buf = (heading + "\n" if heading and not buf.startswith("#") else "") + para
            else:
                buf += ("\n\n" if buf else "") + para
        if buf.strip():
            out.append(buf.strip())
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", help="file or directory of documents")
    ap.add_argument("--mnemos-url", default=os.environ.get("MNEMOS_API_URL", "http://localhost:5002"))
    ap.add_argument("--api-key", default=os.environ.get("MNEMOS_API_KEY", ""))
    ap.add_argument("--category", default="reference")
    ap.add_argument("--source-tag", default="docling")
    ap.add_argument("--max-chars", type=int, default=1800)
    ap.add_argument("--emit-mif", metavar="OUT.jsonld",
                    help="write a MIF/JSON-LD bundle instead of POSTing (import via `mnemos mif import`)")
    args = ap.parse_args()

    root = Path(args.path)
    files = [root] if root.is_file() else sorted(f for f in root.rglob("*") if f.suffix.lower() in DOC_EXTS)
    if not files:
        print(f"no documents ({sorted(DOC_EXTS)}) under {root}", file=sys.stderr)
        return 2

    memories: list[dict] = []
    for f in files:
        print(f"[docling] {f}", file=sys.stderr)
        try:
            md = to_markdown(f)
        except Exception as e:  # one bad doc must not sink the batch
            print(f"  ! skipped ({e})", file=sys.stderr)
            continue
        for i, chunk in enumerate(chunk_by_headings(md, args.max_chars)):
            memories.append({
                "content": chunk,
                "category": args.category,
                "metadata": {"source": args.source_tag, "document": f.name, "chunk": i},
            })
    print(f"[docling] {len(memories)} chunk(s) from {len(files)} document(s)", file=sys.stderr)

    if args.emit_mif:
        # Minimal MIF/JSON-LD memory bundle (vendor-neutral hand-off).
        bundle = {"@context": "https://mif-spec.dev/schema/",
                  "@type": "MemoryCollection", "memories": memories}
        Path(args.emit_mif).write_text(json.dumps(bundle, indent=2))
        print(f"[mif] wrote {args.emit_mif} — import with: mnemos mif import --in {args.emit_mif}")
        return 0

    import requests
    headers = {"content-type": "application/json"}
    if args.api_key:
        headers["authorization"] = f"Bearer {args.api_key}"
    # /v1/memories/bulk collects per-item errors instead of raising.
    r = requests.post(f"{args.mnemos_url}/v1/memories/bulk",
                      headers=headers, data=json.dumps({"memories": memories}), timeout=300)
    r.raise_for_status()
    res = r.json()
    print(f"[mnemos] created={len(res.get('created_ids', []))} errors={len(res.get('errors', []))}")
    for e in res.get("errors", [])[:5]:
        print(f"  ! {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
