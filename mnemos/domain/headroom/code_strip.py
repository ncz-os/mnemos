"""Fenced-code compression placeholder for HEADROOM.

The original HEADROOM prototype included language-aware AST/code stripping.
That path is intentionally not reimplemented in this phase because comment
removal is not universally lossless: comments, shebangs, encoding cookies,
license banners, doctests, markdown examples, and language-specific trivia can
carry semantics for prompts.  This module therefore exposes an explicit safe
no-op so callers can see that code-strip was considered but not applied.
"""

from __future__ import annotations


def strip_fenced_code_lossless(text: str) -> tuple[str, bool]:
    """Return ``text`` unchanged and ``False`` because no safe transform exists yet."""

    return text, False
