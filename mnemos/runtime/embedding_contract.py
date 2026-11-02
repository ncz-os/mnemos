"""F-6 — the embedding-dimension contract.

Field problem the directory already knows about: the PROTEUS replica carried
24,986 NULL embeddings in a ``vector(768)`` column while the fleet standard is
bge-m3 at 1024 dimensions. Three separate silent paths made that possible:

* the shipped defaults disagreed — ``MNEMOS_EMBEDDING_DIM`` defaulted to 768
  while the HTTP embedding model defaulted to ``bge-m3`` (1024-D);
* nothing compared the embedder's *discovered* output width with the
  configured width, so a wrong model simply made every write fail the
  dimension guard and every search fall back to FTS;
* a row whose vector came from a different model was indistinguishable from
  a row that was never embedded, because ``embedding_model`` was never
  recorded at write time.

This module owns the three answers the rest of the codebase needs:

1. :func:`known_model_width` — the output width of a *named* embedding model,
   with the spellings the fleet actually writes (``bge-m3``,
   ``BAAI/bge-m3``, ``/models/bge-m3.gguf``, ``bge-m3-Q8_0.gguf``,
   ``bge-m3:latest`` are all the same model);
2. :func:`embedding_identity` — the ONE resolved (model, width) pair every
   write, backfill and startup check must use, so a model name and a width
   can never come from two different places;
3. :func:`check_embedding_contract` — the startup cross-check that fails
   loud, with one actionable message, when the embedder's discovered width,
   the configured width and the live column width disagree.

The width table lives here (not in ``mnemos.core.config``) because the
import-linter contract puts ``mnemos.core`` BELOW ``mnemos.runtime``'s
neighbours only by layer, and ``core.config`` must not import the runtime
embedder. The configuration side therefore asks THIS module for the
default, and this module asks ``core.config`` for the *environment* — the
dependency arrow stays one-way.

Nothing here touches a database: the live column width is read by the
persistence layer and handed in.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping

logger = logging.getLogger(__name__)

#: The fleet standard embedding model. 1024 dimensions.
FLEET_EMBEDDING_MODEL = "bge-m3"

#: The fleet standard embedding width, in dimensions.
FLEET_EMBEDDING_DIM = 1024

#: Output width per KNOWN embedding model, keyed by the canonical model
#: family. The fleet writes ``bge-m3`` four different ways
#: (``bge-m3``, ``BAAI/bge-m3``, ``/models/bge-m3.gguf``,
#: ``bge-m3-Q8_0.gguf``) and pins a tag (``bge-m3:latest``); all of them
#: name the same 1024-dimensional model, so the lookup normalizes the
#: spelling rather than requiring one canonical string at every call site.
#:
#: An UNKNOWN model returns ``None`` — never a guess. A wrong guess would
#: size a vector column for a model that cannot produce it, which is the
#: exact failure this contract exists to prevent.
KNOWN_MODEL_WIDTHS: dict[str, int] = {
    "bge-m3": 1024,
    "bge-large-en-v1.5": 1024,
    "bge-large-zh-v1.5": 1024,
    "nomic-embed-text": 768,
    "bge-base-en-v1.5": 768,
    "bge-base-zh-v1.5": 768,
    "bge-small-en-v1.5": 384,
    "bge-small-zh-v1.5": 512,
    "e5-base-v2": 768,
    "e5-small-v2": 384,
    "all-minilm-l6-v2": 384,
    "all-mpnet-base-v2": 768,
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "gte-large": 1024,
    "gte-base": 768,
    "gte-small": 384,
    "mxbai-embed-large": 1024,
}

#: Model names that carry a dimension in their spelling (``...-768``).
_DIM_IN_NAME_RE = re.compile(r"^(?P<stem>.+?)-(?P<dim>\d{3,4})$")

#: Quantization/precision suffixes the fleet writes after a model name.
_QUANT_SUFFIXES = (
    "-q8_0",
    "-q5_0",
    "-q4_0",
    "-q4_k_m",
    "-f16",
    "-f32",
    "-iq2_xxs",
    "-iq3_xxs",
    "-iq4_xs",
    "-iq4_nl",
    "-q3_k_l",
    "-q2_k",
)


def _normalize_model_name(model: str | None) -> str:
    """Reduce a model reference to the bare family name.

    Strips a registry/org prefix (``BAAI/bge-m3`` -> ``bge-m3``), a path
    (``/models/bge-m3.gguf`` -> ``bge-m3``), a file extension, a
    quantization suffix (``bge-m3-Q8_0`` -> ``bge-m3``), a container tag
    (``bge-m3:latest`` -> ``bge-m3``) and a trailing ``-768``-style width
    (``bge-base-en-v1.5-768`` -> ``bge-base-en-v1.5``).
    """
    if not model:
        return ""
    name = str(model).strip().lower()
    if not name:
        return ""
    # A path or a bare filename: keep the last component.
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    # A container tag is not part of the model identity.
    name = name.split(":", 1)[0]
    # A file extension is not part of it either.
    for suffix in (".gguf", ".onnx", ".safetensors", ".bin", ".pt"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    # An org/registry prefix.
    name = name.rsplit("/", 1)[-1]
    # A quantization/precision suffix (longest first, so ``-q4_k_m`` wins
    # over ``-q4_0``-style prefixes).
    for suffix in sorted(_QUANT_SUFFIXES, key=len, reverse=True):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    # A width baked into the name (``bge-base-en-v1.5-768``) is redundant
    # once the family is known, but only strip it when what remains is
    # itself a known model — otherwise the width is the only information
    # the name carries and dropping it would lose the answer.
    match = _DIM_IN_NAME_RE.match(name)
    if match:
        stem = match.group("stem")
        if stem in KNOWN_MODEL_WIDTHS:
            name = stem
        else:
            # An unknown family with an explicit width: the width IS the
            # model's output width.
            return f"{stem}-{match.group('dim')}"
    return name


def known_model_width(model: str | None = None) -> int | None:
    """Return the output width of a named embedding model, or ``None``.

    ``model=None`` resolves the CONFIGURED model (the HTTP embedding model
    by default), so the caller never has to repeat the resolution rule.

    ``None`` means "this model's width is not known here" — the caller must
    not substitute a default, because a wrong width sizes a column for a
    model that cannot fill it.
    """
    if model is None:
        from mnemos.core.config import embed_http_model_env

        model = embed_http_model_env()
    name = _normalize_model_name(model)
    if not name:
        return None
    if name in KNOWN_MODEL_WIDTHS:
        return KNOWN_MODEL_WIDTHS[name]
    # An unknown family that still carries an explicit width.
    match = _DIM_IN_NAME_RE.match(name)
    if match:
        try:
            width = int(match.group("dim"))
        except ValueError:  # pragma: no cover - the regex only admits digits
            return None
        return width if width > 0 else None
    return None


@dataclass(frozen=True)
class EmbeddingIdentity:
    """The ONE resolved (model, width) pair every write path must use.

    ``source`` records where the width came from, so a caller can tell a
    measured width from a configured one:

    * ``http_model``  — the width of the named model (authoritative);
    * ``configured``  — the operator's configured width (used only when the
      model's width is unknown);
    * ``unknown``     — neither is known; the width is ``None`` and the
      caller must not invent one.
    """

    model: str
    dim: int | None
    source: str

    @property
    def known(self) -> bool:
        return self.dim is not None


def embedding_identity(
    *,
    http_model: str | None = None,
    configured_dim: int | None = None,
) -> EmbeddingIdentity:
    """Resolve the model name and the width from ONE source.

    A configured width the model cannot produce is reported as the MODEL's
    width, never as the configured one: the embedder is the truth about what
    it writes, and a column sized for the configured value would silently
    reject every vector the model produces.
    """
    if http_model is None:
        from mnemos.core.config import embed_http_model_env

        http_model = embed_http_model_env()
    model = str(http_model or "").strip()
    width = known_model_width(model)
    if width is not None:
        return EmbeddingIdentity(model=model, dim=width, source="http_model")
    if configured_dim is not None:
        return EmbeddingIdentity(model=model, dim=int(configured_dim), source="configured")
    return EmbeddingIdentity(model=model, dim=None, source="unknown")


def embedding_identity_from_settings(settings: Any) -> EmbeddingIdentity:
    """Resolve the identity from a live ``Settings`` object."""
    configured_dim = None
    database = getattr(settings, "database", None)
    if database is not None:
        configured_dim = getattr(database, "embedding_dim", None)
    http_model = None
    providers = getattr(settings, "providers", None)
    if providers is not None:
        http_model = getattr(providers, "inference_embed_model", None) or None
    if not http_model:
        from mnemos.core.config import embed_http_model_env

        http_model = embed_http_model_env()
    return embedding_identity(http_model=http_model, configured_dim=configured_dim)


@dataclass(frozen=True)
class EmbeddingContractReport:
    """The outcome of the startup cross-check."""

    ok: bool
    embedder_dim: int | None
    configured_dim: int | None
    column_dim: int | None
    model: str
    probe_error: str | None = None

    def __bool__(self) -> bool:  # pragma: no cover - convenience only
        return self.ok


#: The knob that turns the cross-check off entirely.
EMBEDDING_DIM_CHECK_ENV = "MNEMOS_EMBEDDING_DIM_CHECK"


def _check_enabled() -> bool:
    from mnemos.core.config import embedding_dim_check_env

    return embedding_dim_check_env().strip().lower() not in ("0", "off", "false", "no")


def _mismatch_message(
    *,
    model: str,
    embedder_dim: int | None,
    configured_dim: int | None,
    column_dim: int | None,
) -> str:
    """One actionable message naming every number that disagreed."""
    parts = []
    if embedder_dim is not None:
        parts.append(f"the embedder '{model}' produces {embedder_dim}-dimensional vectors")
    if configured_dim is not None:
        parts.append(f"MNEMOS_EMBEDDING_DIM is {configured_dim}")
    if column_dim is not None:
        parts.append(f"the live embedding column is sized for {column_dim} dimensions")
    observed = " but ".join(parts) if parts else "the widths in play could not be determined"
    return (
        f"Embedding dimension mismatch: {observed}. "
        "Refusing to serve, because a vector the embedder cannot produce is "
        "indistinguishable from a row that was never embedded. "
        f"Re-embed (or backfill) the rows at the embedder's width, or set "
        f"MNEMOS_EMBEDDING_DIM to the width the embedder actually produces "
        f"({embedder_dim if embedder_dim is not None else 'the model width'}). "
        "A re-embed or a backfill is required before changing the embedding "
        "model or dimension."
    )


async def check_embedding_contract(
    *,
    probe: Callable[[], Awaitable[int | None]],
    configured_dim: int | None,
    model: str,
    column_dim: int | None = None,
    backend: Any | None = None,
) -> EmbeddingContractReport:
    """Cross-check the embedder, the configuration and the live column.

    ``probe`` is called (unless the check is switched off) and must answer
    the embedder's output width, or ``None`` when it could not. A probe
    that could not answer is NOT evidence of a mismatch: it fails open,
    and says so in ``probe_error``.

    ``column_dim`` is the live column width the persistence layer read;
    when it is ``None`` and a ``backend`` was given, the backend is asked
    for it through the ``embedding_column_dim`` repository method.
    """
    if not _check_enabled():
        return EmbeddingContractReport(
            ok=True,
            embedder_dim=None,
            configured_dim=configured_dim,
            column_dim=column_dim,
            model=model,
        )

    probe_error: str | None = None
    embedder_dim: int | None = None
    try:
        embedder_dim = await probe()
    except Exception as exc:
        probe_error = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "[EMBED] embedding dimension probe failed (%s); "
            "skipping the embedder half of the contract check",
            probe_error,
        )

    if column_dim is None and backend is not None:
        reader = getattr(backend, "embedding_column_dim", None)
        if reader is not None:
            try:
                column_dim = await reader()
            except Exception as exc:
                probe_error = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "[EMBED] live embedding column width could not be read (%s)",
                    probe_error,
                )

    report = EmbeddingContractReport(
        ok=True,
        embedder_dim=embedder_dim,
        configured_dim=configured_dim,
        column_dim=column_dim,
        model=model,
        probe_error=probe_error,
    )

    # The embedder is the truth. When it answered, everything else must
    # agree with it.
    if embedder_dim is not None:
        if configured_dim is not None and int(configured_dim) != int(embedder_dim):
            raise RuntimeError(
                _mismatch_message(
                    model=model,
                    embedder_dim=embedder_dim,
                    configured_dim=configured_dim,
                    column_dim=column_dim,
                )
            )
        if column_dim is not None and int(column_dim) != int(embedder_dim):
            raise RuntimeError(
                _mismatch_message(
                    model=model,
                    embedder_dim=embedder_dim,
                    configured_dim=configured_dim,
                    column_dim=column_dim,
                )
            )
    elif column_dim is not None and configured_dim is not None and int(column_dim) != int(configured_dim):
        # The probe could not answer, but the configuration and the live
        # column still have to agree with each other.
        raise RuntimeError(
            _mismatch_message(
                model=model,
                embedder_dim=None,
                configured_dim=configured_dim,
                column_dim=column_dim,
            )
        )
    return report


async def verify_startup_embedding_contract(
    backend: Any,
    settings: Any,
    *,
    probe: Callable[[], Awaitable[int | None]] | None = None,
) -> EmbeddingContractReport:
    """The App-lifespan wiring helper: refuse to serve a mismatched embedder.

    Reads the configured width and the model from ``settings`` and the live
    column width from ``backend``, then runs the same cross-check the
    tests pin.
    """
    configured_dim = None
    database = getattr(settings, "database", None)
    if database is not None:
        configured_dim = getattr(database, "embedding_dim", None)
    identity = embedding_identity_from_settings(settings)

    if probe is None:
        probe = _default_probe

    return await check_embedding_contract(
        probe=probe,
        configured_dim=configured_dim,
        model=identity.model,
        backend=backend,
    )


async def _default_probe() -> int | None:
    """Measure the live embedder's output width.

    Returns ``None`` when the embedder cannot be reached or produces
    nothing measurable — a probe that cannot answer is not evidence of a
    mismatch.
    """
    from mnemos.core.config import embed_http_model_env

    model = embed_http_model_env()
    known = known_model_width(model)
    try:
        embedder = get_embedder()
        vector = await embedder.embed("embedding dimension probe")
    except Exception as exc:
        logger.info("[EMBED] could not probe the embedder's output width (%s)", exc)
        return known
    if not vector:
        return known
    return len(vector)


def model_identity_fields(
    identity: EmbeddingIdentity,
) -> Mapping[str, Any]:
    """The fields a write path records beside a vector.

    Recording the model and the width at write time is what makes a row
    that was never embedded distinguishable from a row whose vector came
    from a different model.
    """
    return {
        "embedding_model": identity.model or None,
        "embedding_dim": identity.dim,
    }
