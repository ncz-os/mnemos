"""HEADROOM lossless prompt/token compression primitives.

This package is intentionally a pure importable library.  It does not wire
itself into PANTHEON request dispatch; callers opt in by invoking
:func:`compress` before sending messages/tool arguments to a provider.
"""

from .json_minify import JSONMinifyError, is_json_lossless_equivalent, minify_json_text
from .library import CompressionResult, TransformRecord, compress, compress_messages, compress_text

__all__ = [
    "CompressionResult",
    "JSONMinifyError",
    "TransformRecord",
    "compress",
    "compress_messages",
    "compress_text",
    "is_json_lossless_equivalent",
    "minify_json_text",
]
