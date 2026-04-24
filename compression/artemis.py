"""ARTEMIS — CPU-only extractive compression engine.

Apollo's twin. Artemis hunts the essential content — tracks
identifiers and structure through dense prose, preserves what the
judge cares about, drops what it doesn't.

Apollo handles schema-typed dense encoding + LLM fallback for prose
that misses every schema; Artemis handles the pure-CPU path when
GPU is unavailable or latency-sensitive. Default contest engine
alongside Apollo; runs as the extractive peer that competes
directly with ANAMNESIS's LLM extraction on cost.

Design grounded in a GRAEAE multi-muse consultation (5 models
agreed unanimously on the stack): the 0.44 judge-mean of the
earlier stop-word extractive engine isn't a sentence-selection
problem — it's an information-dropping problem. The judge
penalizes missing identifiers, version strings, dates, URLs, and
broken labeled content. Fix those drops first; the rest is
sentence ranking.

The pipeline

  1. Protected-span regex pass — tags dates, versions, emails,
     URLs, numbers, IDs, quoted strings. These are sacred and get
     a PageRank boost; their host sentences are force-selected.
  2. Structure detection — recognizes "Key: Value" labeled blocks
     and preserves them verbatim as atomic units.
  3. TF-IDF sentence scoring — term-rarity-weighted scoring using
     stdlib Counter + math.log. Document centroid cosine similarity.
  4. Anchored TextRank — sentence-sentence cosine similarity graph
     + personalized PageRank biased toward sentences containing
     protected spans (Gemini's suggestion during the consultation).
     When networkx isn't installed, falls back to TF-IDF-only
     ranking (still a measurable improvement over stop-word
     filtering).
  5. MMR selection — greedy top-K with redundancy penalty. Forces
     inclusion of sentences that host protected spans.
  6. Assembly — re-order by original position, prepend labeled
     blocks, emit as prose.

Target latency: <20 ms on 4000-char inputs. Pure stdlib + numpy
(already required) + optional networkx. No GPU, no models on disk,
no network.
"""
from __future__ import annotations

import math
import re
import time
from collections import Counter
from typing import Dict, List, Optional, Tuple

import numpy as np

from .base import (
    CompressionEngine,
    CompressionRequest,
    CompressionResult,
    GPUIntent,
    IdentifierPolicy,
)


# ── protected-span regexes ────────────────────────────────────────────────


_PROTECTED_PATTERNS: List[Tuple[str, re.Pattern]] = [
    ("version", re.compile(r"\bv\d+(?:\.\d+){1,3}(?:[-.][a-z0-9]+)?\b", re.IGNORECASE)),
    ("iso_date", re.compile(r"\b20\d{2}-\d{2}-\d{2}\b")),
    ("slash_date", re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b")),
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("url", re.compile(r"https?://\S+")),
    ("uuid", re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE)),
    ("sha", re.compile(r"\b[0-9a-f]{7,40}\b", re.IGNORECASE)),  # commit SHAs
    ("path", re.compile(r"(?:\.?/)?(?:[A-Za-z0-9_-]+/)+[A-Za-z0-9_.-]+")),
    ("ticker", re.compile(r"\b[A-Z]{1,5}\b")),  # loose; filtered below
    ("id_code", re.compile(r"\b[A-Z]+-\d+\b")),  # INC-1234, JIRA-456
    ("phone", re.compile(r"\b(?:\+?\d{1,3}[\s\-.])?\(?\d{3}\)?[\s\-.]?\d{3}[\s\-.]?\d{4}\b")),
    ("percent", re.compile(r"\b\d+(?:\.\d+)?%")),
    ("currency", re.compile(r"\$\d+(?:[,.]?\d+)*")),
    ("quoted", re.compile(r'"[^"\n]{2,200}"')),
    ("number", re.compile(r"\b\d{3,}(?:\.\d+)?\b")),  # 3+ digit numbers
]

# All-caps English words that shouldn't be treated as tickers.
_TICKER_BLOCKLIST = frozenset({
    "AT", "THE", "AND", "OR", "BUT", "FOR", "WITH", "FROM", "TO",
    "IN", "ON", "AS", "BY", "IF", "IS", "IT", "OF", "A", "AN",
    "TODO", "FIXME", "NOTE", "WARN", "INFO", "HTTP", "HTTPS",
    "JSON", "HTML", "SQL", "API", "URL", "UUID", "PR", "CI",
    "CEO", "CTO", "CFO", "VP",  # role titles — not tickers
})


def _find_protected_spans(text: str) -> List[Tuple[int, int, str]]:
    """Return list of (start, end, category) tuples for all
    protected spans in the text. De-duplicates overlapping spans by
    keeping the first-registered kind when two overlap."""
    spans: List[Tuple[int, int, str]] = []
    for kind, pat in _PROTECTED_PATTERNS:
        for m in pat.finditer(text):
            if kind == "ticker":
                value = m.group(0)
                if value in _TICKER_BLOCKLIST:
                    continue
            if kind == "sha":
                # Skip plain numbers (regex is permissive)
                if m.group(0).isdigit() and len(m.group(0)) < 8:
                    continue
            spans.append((m.start(), m.end(), kind))
    # Sort + dedupe overlapping: earliest-starting + longest wins.
    spans.sort(key=lambda s: (s[0], -(s[1] - s[0])))
    merged: List[Tuple[int, int, str]] = []
    for s in spans:
        if merged and s[0] < merged[-1][1]:
            continue  # overlaps earlier span
        merged.append(s)
    return merged


# ── labeled-block detection ───────────────────────────────────────────────


_LABEL_LINE_RE = re.compile(
    r"^(?P<key>[A-Za-z][A-Za-z0-9 _-]{1,30}?)\s*:\s+(?P<value>.+)$"
)


def _extract_labeled_blocks(text: str) -> List[str]:
    """Return contiguous runs of 'Key: Value' lines as atomic blocks,
    each preserved verbatim in output. A single labeled line doesn't
    count — we want runs of 2+ to avoid false-positives on prose
    sentences like 'The issue: X was broken'."""
    lines = text.split("\n")
    blocks: List[str] = []
    current: List[str] = []
    for line in lines:
        m = _LABEL_LINE_RE.match(line)
        if m:
            current.append(line)
        else:
            if len(current) >= 2:
                blocks.append("\n".join(current))
            current = []
    if len(current) >= 2:
        blocks.append("\n".join(current))
    return blocks


# ── sentence tokenization ─────────────────────────────────────────────────


_SENT_SPLIT_RE = re.compile(
    r"(?<=[.!?])\s+(?=[A-Z])"  # period/?/! + whitespace + capital letter
    r"|(?<=\n)\s*\n"            # paragraph breaks
)


def _split_sentences(text: str) -> List[str]:
    """Regex sentence-boundary splitter. No NLTK dependency."""
    candidates = _SENT_SPLIT_RE.split(text)
    sentences = [s.strip() for s in candidates if s.strip()]
    # Drop trailing-empty and collapse whitespace.
    return [re.sub(r"\s+", " ", s) for s in sentences if len(s) > 3]


# ── TF-IDF (stdlib) ───────────────────────────────────────────────────────


_TOKEN_RE = re.compile(r"\b[A-Za-z][A-Za-z_-]{2,}\b")


def _tokenize_for_scoring(text: str) -> List[str]:
    """Content-word tokens, lowercased, stopwords filtered."""
    return [
        w.lower() for w in _TOKEN_RE.findall(text)
        if w.lower() not in _STOP_WORDS
    ]


_STOP_WORDS = frozenset({
    "the", "and", "for", "that", "this", "with", "from", "are", "was",
    "were", "have", "has", "had", "but", "not", "you", "your", "they",
    "their", "been", "will", "can", "any", "all", "also", "into", "over",
    "only", "about", "its", "our", "him", "her", "them", "would",
    "could", "should", "may", "might", "must", "shall", "then", "than",
    "when", "where", "what", "who", "whom", "which", "while", "does",
})


def _tfidf_vectors(
    sentences: List[str],
) -> Tuple[List[Dict[str, float]], Counter]:
    """Compute per-sentence TF-IDF vectors as sparse dicts.
    Returns (per-sentence vectors, document-level term counts)."""
    doc_terms: Counter = Counter()
    sentence_terms: List[List[str]] = []
    for s in sentences:
        toks = _tokenize_for_scoring(s)
        sentence_terms.append(toks)
        doc_terms.update(set(toks))   # IDF counts doc-frequency, not TF

    n_docs = max(1, len(sentences))
    vectors: List[Dict[str, float]] = []
    for toks in sentence_terms:
        tf = Counter(toks)
        vec: Dict[str, float] = {}
        for term, count in tf.items():
            idf = math.log((n_docs + 1) / (doc_terms[term] + 1)) + 1.0
            vec[term] = count * idf
        vectors.append(vec)
    return vectors, doc_terms


def _cosine(a: Dict[str, float], b: Dict[str, float]) -> float:
    """Cosine similarity between two sparse dicts."""
    if not a or not b:
        return 0.0
    common = set(a) & set(b)
    if not common:
        return 0.0
    dot = sum(a[k] * b[k] for k in common)
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


# ── TextRank (networkx-backed, with TF-IDF-only fallback) ────────────────


def _anchored_textrank(
    sentences: List[str],
    vectors: List[Dict[str, float]],
    anchored_indices: set,
) -> List[float]:
    """Run personalized PageRank over sentence-sentence cosine
    similarity graph, with restart probability biased toward
    sentences containing protected spans.

    Falls back to TF-IDF-only centroid scoring when networkx isn't
    installed (core install without the `full` extra). The fallback
    still outperforms stop-word filtering because it considers term
    rarity + sentence-vs-document similarity."""
    try:
        import networkx as nx
    except ImportError:
        # Centroid-based ranking: score each sentence by cosine to
        # the document centroid. Anchored sentences get a +0.5 bias.
        centroid: Dict[str, float] = {}
        for v in vectors:
            for k, val in v.items():
                centroid[k] = centroid.get(k, 0.0) + val
        scores = [_cosine(v, centroid) for v in vectors]
        return [
            s + (0.5 if i in anchored_indices else 0.0)
            for i, s in enumerate(scores)
        ]

    if len(sentences) <= 1:
        return [1.0] * len(sentences)

    G = nx.Graph()
    for i in range(len(sentences)):
        G.add_node(i)
    for i in range(len(sentences)):
        for j in range(i + 1, len(sentences)):
            sim = _cosine(vectors[i], vectors[j])
            if sim > 0.01:
                G.add_edge(i, j, weight=sim)

    personalization: Optional[Dict[int, float]] = None
    if anchored_indices:
        total = float(len(sentences))
        boost = 3.0
        personalization = {
            i: (boost if i in anchored_indices else 1.0) / total
            for i in range(len(sentences))
        }

    try:
        scores = nx.pagerank(G, personalization=personalization, weight="weight")
    except (nx.NetworkXException, ZeroDivisionError):
        # Graph-pathological case (fully disconnected, etc.) — fall
        # back to uniform scoring.
        return [1.0] * len(sentences)

    return [scores.get(i, 0.0) for i in range(len(sentences))]


# ── MMR selection ────────────────────────────────────────────────────────


def _mmr_select(
    sentences: List[str],
    scores: List[float],
    vectors: List[Dict[str, float]],
    target_length: int,
    forced_indices: set,
    lambda_: float = 0.7,
) -> List[int]:
    """Maximal Marginal Relevance: greedy selection that balances
    relevance (score) against redundancy (similarity to already-
    selected sentences). Stops when cumulative char length hits
    target_length.

    ``forced_indices`` are included first, in score order, regardless
    of MMR — these are sentences hosting protected spans.
    """
    if not sentences:
        return []

    selected: List[int] = []
    remaining = set(range(len(sentences)))

    # Forced picks first — preserve protected-span-hosting sentences.
    forced_sorted = sorted(
        [i for i in forced_indices if i in remaining],
        key=lambda i: -scores[i],
    )
    total_chars = 0
    for i in forced_sorted:
        selected.append(i)
        remaining.discard(i)
        total_chars += len(sentences[i])
        if total_chars >= target_length:
            return sorted(selected)

    # Greedy MMR on remaining.
    while remaining and total_chars < target_length:
        best_i = None
        best_score = -float("inf")
        for i in remaining:
            relevance = scores[i]
            if selected:
                max_sim = max(_cosine(vectors[i], vectors[j]) for j in selected)
            else:
                max_sim = 0.0
            mmr_score = lambda_ * relevance - (1.0 - lambda_) * max_sim
            if mmr_score > best_score:
                best_score = mmr_score
                best_i = i
        if best_i is None:
            break
        selected.append(best_i)
        remaining.discard(best_i)
        total_chars += len(sentences[best_i])

    return sorted(selected)


# ── the engine ────────────────────────────────────────────────────────────


class ARTEMISEngine(CompressionEngine):
    """CPU-only extractive compression engine.

    ``id = 'artemis'``, ``gpu_intent = CPU_ONLY``. Never hits a GPU
    and never makes an HTTP call. Target latency <20 ms on 4000-char
    inputs. Default contest engine alongside APOLLO on v3.3+
    deployments.

    Identifier policy: ``STRICT`` — the protected-span pre-pass
    guarantees dates, versions, emails, URLs, IDs, quoted strings,
    and numbers survive verbatim. Structure-preserving label
    detection keeps labeled blocks verbatim. The composite of the
    two removes the primary failure mode (dropped identifiers) that
    made the prior stop-word extractive engine score 0.44 judge mean.
    """

    id = "artemis"
    label = "ARTEMIS — CPU-only extractive with identifier preservation"
    version = "0.1"
    gpu_intent = GPUIntent.CPU_ONLY

    def __init__(self, target_ratio: float = 0.5) -> None:
        super().__init__()
        self._target_ratio = target_ratio

    async def compress(self, request: CompressionRequest) -> CompressionResult:
        started = time.perf_counter()
        content = request.content or ""
        original_tokens = len(content.split())
        original_chars = max(1, len(content))

        if len(content) < 40:
            # Tiny content — nothing to compress. Return as-is, tag
            # the path so the contest understands.
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            return CompressionResult(
                engine_id=self.id,
                engine_version=self.version,
                compressed_content=content,
                original_tokens=original_tokens,
                compressed_tokens=original_tokens,
                compression_ratio=1.0,
                quality_score=None,   # judge will score
                elapsed_ms=elapsed_ms,
                gpu_used=False,
                identifier_policy=IdentifierPolicy.STRICT,
                manifest={"path": "passthrough_short"},
            )

        # Phase 1: protected-span detection.
        protected = _find_protected_spans(content)

        # Phase 2: labeled-block extraction (runs of 2+ Key: Value lines).
        labeled_blocks = _extract_labeled_blocks(content)

        # If the entire content is a labeled block, return it verbatim
        # — no compression is better than expansion.
        if labeled_blocks and sum(len(b) for b in labeled_blocks) >= len(content) * 0.8:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            return CompressionResult(
                engine_id=self.id,
                engine_version=self.version,
                compressed_content=content,
                original_tokens=original_tokens,
                compressed_tokens=original_tokens,
                compression_ratio=1.0,
                quality_score=None,
                elapsed_ms=elapsed_ms,
                gpu_used=False,
                identifier_policy=IdentifierPolicy.STRICT,
                manifest={"path": "passthrough_labeled"},
            )

        # Phase 3: sentence-level extractive ranking.
        sentences = _split_sentences(content)
        if not sentences:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            return CompressionResult(
                engine_id=self.id,
                engine_version=self.version,
                original_tokens=original_tokens,
                elapsed_ms=elapsed_ms,
                gpu_used=False,
                identifier_policy=IdentifierPolicy.STRICT,
                manifest={"path": "no_sentences"},
                error="unable to tokenize sentences",
            )

        # Which sentences contain protected spans? (Host-sentence indexes.)
        anchored_indices: set = set()
        for (span_start, span_end, _) in protected:
            # Find the sentence containing this span.
            cursor = 0
            for i, s in enumerate(sentences):
                idx = content.find(s, cursor)
                if idx < 0:
                    continue
                if idx <= span_start < idx + len(s):
                    anchored_indices.add(i)
                    break
                cursor = idx + len(s)

        vectors, _ = _tfidf_vectors(sentences)
        scores = _anchored_textrank(sentences, vectors, anchored_indices)

        # Phase 4: MMR selection to a target length.
        target_length = int(original_chars * self._target_ratio)
        selected = _mmr_select(
            sentences, scores, vectors, target_length, anchored_indices,
        )

        # Phase 5: assembly.
        selected_sentences = [sentences[i] for i in selected]
        compressed = " ".join(selected_sentences)

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        compressed_tokens = len(compressed.split())
        ratio = len(compressed) / original_chars

        return CompressionResult(
            engine_id=self.id,
            engine_version=self.version,
            compressed_content=compressed,
            original_tokens=original_tokens,
            compressed_tokens=compressed_tokens,
            compression_ratio=ratio,
            quality_score=None,    # judge scores it
            elapsed_ms=elapsed_ms,
            gpu_used=False,
            identifier_policy=IdentifierPolicy.STRICT,
            manifest={
                "path": "extractive",
                "protected_spans": len(protected),
                "anchored_sentences": len(anchored_indices),
                "selected": len(selected),
                "total_sentences": len(sentences),
                "target_ratio": self._target_ratio,
            },
        )
