"""CHARON backend-aware export/import — round-trip tests on the SQLite backend.

The MIF concept layer is exercised in :mod:`tests.test_charon_bundle` /
:mod:`tests.test_mif_portability` (pure mapping, no backend). This module
covers the NEW layer: :func:`charon.export_bundle_from_backend` and
:func:`charon.import_bundle_to_backend` — the additive functions that walk
a real :class:`PersistenceBackend` and persist KG triples / memory
versions / compressed variants alongside the memory concepts.

SQLite is the proxy backend (per the deliverable: round-trip the full set
on SQLite; cross-backend live tests aren't free). The repo abstraction is
identical across backends, so a SQLite round-trip exercises the same code
path the Postgres / Oracle / Db2 / MySQL backends run — the backend
contract ``memories`` / ``kg_triples`` / ``memory_versions`` /
``compression`` + ``transactional()`` is universal.

Conventions:

* every test seeds ``src`` with a known mix (memories + kg + versions +
  compression), exports to ``bundle``, then imports into ``dst`` and
  asserts the rows landed.
* vault content is checked: vault memories are redacted in the bundle
  AND the import path respects the vault namespace.
* the manifest's ``sidecars`` block is asserted to record presence +
  counts.
* no backend-specific SQL appears in :mod:`mnemos.portability.charon`;
  these tests don't reach into driver code either.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio

from mnemos.core.secret_detection import VAULT_NAMESPACE
from mnemos.portability import charon, mif


# ── fixtures + helpers ─────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def sqlite_backend(tmp_path):
    from mnemos.persistence.sqlite import SqliteBackend

    backend = SqliteBackend(tmp_path / "charon_roundtrip.sqlite3", SimpleNamespace())
    await backend.open()
    try:
        yield backend
    finally:
        await backend.close()


def _utcnow():
    return datetime.now(timezone.utc)


async def _seed_memory(
    backend,
    *,
    memory_id: str | None = None,
    content: str = "MNEMOS adopts MIF natively.",
    category: str = "decisions",
    subcategory: str | None = None,
    owner_id: str = "alice",
    namespace: str = "default",
    permission_mode: int = 0,
    metadata: dict | None = None,
    quality_rating: int = 80,
    source_model: str | None = None,
    source_provider: str | None = None,
    source_session: str | None = None,
    source_agent: str | None = None,
    verbatim_content: str | None = None,
    embedding: list[float] | None = None,
    created: datetime | None = None,
    updated: datetime | None = None,
):
    mid = memory_id or f"mem_{uuid.uuid4().hex[:12]}"
    created_at = created or _utcnow()
    updated_at = updated or created_at
    async with backend.transactional() as tx:
        await backend.memories.insert_memory(
            tx,
            memory_id=mid,
            content=content,
            category=category,
            subcategory=subcategory,
            metadata_json=json.dumps(metadata or {}),
            quality_rating=quality_rating,
            owner_id=owner_id,
            namespace=namespace,
            permission_mode=permission_mode,
            source_model=source_model,
            source_provider=source_provider,
            source_session=source_session,
            source_agent=source_agent,
            verbatim_content=verbatim_content if verbatim_content is not None else content,
            embedding=embedding,
            created=created_at,
            updated=updated_at,
        )
    return mid


async def _update_memory_fields(backend, memory_id: str, **fields):
    from mnemos.persistence.visibility import VisibilityFilter, VisibilityScope

    vis = VisibilityFilter(
        scope=VisibilityScope.ROOT_BYPASS,
        user_id=None,
        group_ids=(),
        namespace=None,
        exclude_namespaces=(),
    )
    async with backend.transactional() as tx:
        return await backend.memories.update_memory(tx, memory_id, visibility=vis, fields=fields)


async def _seed_kg_triple(
    backend,
    *,
    triple_id: str | None = None,
    subject: str,
    predicate: str,
    obj: str,
    memory_id: str | None = None,
    subject_type: str | None = "entity",
    object_type: str | None = "entity",
    owner_id: str = "alice",
    namespace: str = "default",
    confidence: float | None = 0.9,
):
    tid = triple_id or f"t_{uuid.uuid4().hex[:10]}"
    async with backend.transactional() as tx:
        await backend.kg_triples.insert_kg_triple(
            tx,
            triple_id=tid,
            subject=subject,
            predicate=predicate,
            obj=obj,
            subject_type=subject_type,
            object_type=object_type,
            valid_from=None,
            valid_until=None,
            memory_id=memory_id,
            confidence=confidence,
            created=None,
            owner_id=owner_id,
            namespace=namespace,
        )
    return tid


async def _seed_memory_version(
    backend,
    *,
    version_id: str | None = None,
    memory_id: str,
    version_num: int,
    content: str,
    owner_id: str = "alice",
    namespace: str = "default",
    branch: str = "main",
    change_type: str = "create",
    parent_version_id: str | None = None,
    merge_parents: list[str] | None = None,
    metadata: dict | None = None,
):
    vid = version_id or f"v_{uuid.uuid4().hex[:10]}"
    async with backend.transactional() as tx:
        await backend.memory_versions.insert_memory_version(
            tx,
            version_id=vid,
            memory_id=memory_id,
            version_num=version_num,
            content=content,
            category="decisions",
            subcategory=None,
            metadata_json=json.dumps(metadata or {}),
            verbatim_content=content,
            owner_id=owner_id,
            namespace=namespace,
            permission_mode=0,
            source_model=None,
            source_provider=None,
            source_session=None,
            source_agent=None,
            snapshot_at=_utcnow(),
            snapshot_by=owner_id,
            change_type=change_type,
            commit_hash=uuid.uuid4().hex,
            parent_version_id=parent_version_id,
            branch=branch,
            merge_parents=merge_parents or [],
        )
    return vid


async def _seed_compressed_variant(
    backend,
    *,
    memory_id: str,
    owner_id: str = "alice",
    engine_id: str = "engine",
    engine_version: str = "1",
    compressed_content: str = "short",
    compressed_tokens: int = 1,
    compression_ratio: float = 0.5,
    quality_score: float = 0.9,
    composite_score: float = 0.8,
    scoring_profile: str = "balanced",
    judge_model: str = "judge",
):
    async with backend.transactional() as tx:
        return await backend.compression.insert_compressed_variant(
            tx,
            memory_id=memory_id,
            owner_id=owner_id,
            winner_candidate_id=None,
            engine_id=engine_id,
            engine_version=engine_version,
            compressed_content=compressed_content,
            compressed_tokens=compressed_tokens,
            compression_ratio=compression_ratio,
            quality_score=quality_score,
            composite_score=composite_score,
            scoring_profile=scoring_profile,
            judge_model=judge_model,
            selected_at=_utcnow(),
        )


async def _list_kg_triples(backend):
    """Backend-neutral read of all KG triples (test helper; one-off scan)."""
    async with backend.transactional() as tx:
        # ``list_triples`` isn't on the repo surface; tests just look the
        # triples up by id. Use the per-id fetcher.
        return tx  # placeholder; real fetch happens per-id below


async def _get_kg_triple(backend, triple_id: str):
    async with backend.transactional() as tx:
        return await backend.kg_triples.fetch_kg_triple_by_id(tx, triple_id)


async def _get_memory_version(backend, version_id: str):
    async with backend.transactional() as tx:
        return await backend.memory_versions.fetch_memory_version_by_id(tx, version_id)


async def _get_memory_log(backend, memory_id: str, *, branch: str = "main", limit: int = 10):
    from mnemos.core.auth_context import UserContext

    root = UserContext(
        user_id="root",
        group_ids=[],
        role="root",
        namespace="default",
        authenticated=True,
    )
    async with backend.transactional() as tx:
        return await backend.memories.fetch_memory_log(tx, memory_id, branch, limit, root)


async def _get_compressed_variant(backend, memory_id: str):
    async with backend.transactional() as tx:
        return await backend.compression.fetch_compressed_variant_by_memory_id(tx, memory_id)


async def _get_memory(backend, memory_id: str):
    from mnemos.persistence.visibility import VisibilityFilter, VisibilityScope

    vis = VisibilityFilter(
        scope=VisibilityScope.ROOT_BYPASS,
        user_id=None,
        group_ids=(),
        namespace=None,
        exclude_namespaces=(),
    )
    async with backend.transactional() as tx:
        return await backend.memories.get_memory(tx, memory_id, visibility=vis, include_archived=True)


async def _get_memory_export_row(backend, memory_id: str):
    async with backend.transactional() as tx:
        rows = await backend.memories.fetch_memory_export(
            tx,
            effective_owner=None,
            effective_ns=None,
            category=None,
            limit=100,
            offset=0,
        )
    return next((row for row in rows if row["id"] == memory_id), None)


# ── export_bundle_from_backend: happy path ──────────────────────────────────


@pytest.mark.asyncio
async def test_export_bundle_from_backend_writes_concepts_and_manifest(sqlite_backend, tmp_path):
    await _seed_memory(sqlite_backend, content="MNEMOS goes native MIF.", namespace="default")
    await _seed_memory(
        sqlite_backend,
        content="Restart the gateway after the schema migration.",
        category="rules",
        namespace="default",
    )

    out_dir = tmp_path / "bundle"
    manifest = await charon.export_bundle_from_backend(
        sqlite_backend,
        out_dir,
        include_sidecars=False,
    )

    # concept layer + manifest
    assert manifest["count"] == 2
    assert (out_dir / charon.MANIFEST_NAME).is_file()
    md = list(out_dir.rglob("*.md"))
    assert len(md) == 2
    # sidecar section absent when not requested
    assert manifest["sidecars_included"] is False
    assert "sidecars" not in manifest
    # backend name recorded
    assert manifest["backend"] == "SqliteBackend"


@pytest.mark.asyncio
async def test_export_bundle_from_backend_excludes_vault_by_default(sqlite_backend, tmp_path):
    """ROOT_BYPASS export must NEVER leak vault memories (F1 posture)."""
    await _seed_memory(
        sqlite_backend,
        content="INFRASTRUCTURE CREDENTIALS host TYPHON password Gumbo@Kona1b",
        namespace=VAULT_NAMESPACE,
    )
    await _seed_memory(sqlite_backend, content="ordinary note", namespace="default")

    out_dir = tmp_path / "bundle"
    await charon.export_bundle_from_backend(sqlite_backend, out_dir, include_sidecars=False)

    # Vault content MUST NOT survive into the bundle.
    blob = "\n".join(p.read_text() for p in out_dir.rglob("*.md"))
    assert "Gumbo@Kona1b" not in blob
    # Vault redaction marker IS in the bundle for any (root opt-in) vault
    # rows — but with our default filter no vault rows are exported at all.
    # Either way: no secret survives.
    assert "ordinary note" in blob


@pytest.mark.asyncio
async def test_export_bundle_from_backend_paginates(sqlite_backend, tmp_path):
    """A multi-page list_memories walk converges on the full row count."""
    seeded = []
    for i in range(7):
        mid = await _seed_memory(sqlite_backend, content=f"memory number {i}", namespace="default")
        seeded.append(mid)

    # Force a small page so the pagination loop iterates.
    out_dir = tmp_path / "bundle"
    manifest = await charon.export_bundle_from_backend(
        sqlite_backend, out_dir, include_sidecars=False, page_size=2
    )
    assert manifest["count"] == 7
    md = sorted(p.name for p in out_dir.rglob("*.md"))
    assert len(md) == 7
    # every seeded id has a corresponding concept file
    for mid in seeded:
        assert any(mid in (p.read_text()) for p in out_dir.rglob("*.md"))


# ── export_bundle_from_backend: sidecars ────────────────────────────────────


@pytest.mark.asyncio
async def test_export_bundle_from_backend_writes_kg_triples_sidecar(sqlite_backend, tmp_path):
    mid_a = await _seed_memory(sqlite_backend, content="Athena's wisdom", namespace="default")
    await _seed_memory(sqlite_backend, content="Independent fact", namespace="default")

    await _seed_kg_triple(
        sqlite_backend,
        subject="Athena",
        predicate="guides",
        obj="Odysseus",
        memory_id=mid_a,
    )
    await _seed_kg_triple(
        sqlite_backend,
        subject="Hermes",
        predicate="visits",
        obj="Ithaca",
        memory_id=None,
    )

    out_dir = tmp_path / "bundle"
    manifest = await charon.export_bundle_from_backend(
        sqlite_backend, out_dir, include_sidecars=True
    )

    sidecar_dir = out_dir / charon.SIDECAR_DIR
    assert sidecar_dir.is_dir()
    kg_path = sidecar_dir / charon.KG_TRIPLES_SIDECAR
    assert kg_path.is_file()

    # Manifest records presence + count + the relative path consumers use.
    assert manifest["sidecars_included"] is True
    assert manifest["sidecars"]["kg_triples"]["count"] == 2
    assert manifest["sidecars"]["kg_triples"]["truncated"] is False
    assert manifest["sidecars"]["kg_triples"]["path"] == f"{charon.SIDECAR_DIR}/{charon.KG_TRIPLES_SIDECAR}"

    # The unattached triple survives (include_unattached=True).
    triples = [json.loads(line) for line in kg_path.read_text().splitlines() if line.strip()]
    subjects = {t["subject"] for t in triples}
    assert {"Athena", "Hermes"} <= subjects
    # The memory_id on the attached triple points back at the memory row.
    attached = next(t for t in triples if t["subject"] == "Athena")
    assert attached["memory_id"] == mid_a
    assert attached["id"]  # the triple id survives too


@pytest.mark.asyncio
async def test_export_bundle_from_backend_drops_unattached_vault_kg_triples(sqlite_backend, tmp_path):
    await _seed_memory(sqlite_backend, content="ordinary note", namespace="default")
    await _seed_kg_triple(
        sqlite_backend,
        subject="VaultCredential",
        predicate="stores",
        obj="ROOT_PW=Gumbo@Kona1b",
        memory_id=None,
        namespace=VAULT_NAMESPACE,
    )
    await _seed_kg_triple(
        sqlite_backend,
        subject="Hermes",
        predicate="visits",
        obj="Ithaca",
        memory_id=None,
        namespace="default",
    )

    out_dir = tmp_path / "bundle"
    manifest = await charon.export_bundle_from_backend(sqlite_backend, out_dir, include_sidecars=True)

    kg_path = out_dir / charon.SIDECAR_DIR / charon.KG_TRIPLES_SIDECAR
    triples = [json.loads(line) for line in kg_path.read_text().splitlines() if line.strip()]
    assert manifest["sidecars"]["kg_triples"]["count"] == 1
    assert {row["subject"] for row in triples} == {"Hermes"}
    assert "Gumbo@Kona1b" not in kg_path.read_text()


@pytest.mark.asyncio
async def test_export_bundle_from_backend_writes_memory_versions_sidecar(sqlite_backend, tmp_path):
    mid = await _seed_memory(sqlite_backend, content="a memory", namespace="default")
    v1 = await _seed_memory_version(
        sqlite_backend, memory_id=mid, version_num=1, content="first", change_type="create"
    )
    v2 = await _seed_memory_version(
        sqlite_backend,
        memory_id=mid,
        version_num=2,
        content="second",
        change_type="update",
        parent_version_id=v1,
    )

    out_dir = tmp_path / "bundle"
    manifest = await charon.export_bundle_from_backend(
        sqlite_backend, out_dir, include_sidecars=True
    )

    sidecar = out_dir / charon.SIDECAR_DIR / charon.MEMORY_VERSIONS_SIDECAR
    assert sidecar.is_file()
    assert manifest["sidecars"]["memory_versions"]["count"] == 2
    versions = [json.loads(line) for line in sidecar.read_text().splitlines() if line.strip()]
    by_id = {v["id"]: v for v in versions}
    assert v1 in by_id and v2 in by_id
    assert by_id[v2]["parent_version_id"] == v1
    assert by_id[v1]["version_num"] == 1
    assert by_id[v2]["version_num"] == 2
    assert by_id[v1]["branch"] == "main"


@pytest.mark.asyncio
async def test_export_bundle_from_backend_writes_compression_sidecar(sqlite_backend, tmp_path):
    mid = await _seed_memory(sqlite_backend, content="a memory", namespace="default")
    await _seed_compressed_variant(
        sqlite_backend,
        memory_id=mid,
        compressed_content="short summary",
        compression_ratio=0.25,
        quality_score=0.92,
    )

    out_dir = tmp_path / "bundle"
    manifest = await charon.export_bundle_from_backend(
        sqlite_backend, out_dir, include_sidecars=True
    )

    sidecar = out_dir / charon.SIDECAR_DIR / charon.COMPRESSION_SIDECAR
    assert sidecar.is_file()
    assert manifest["sidecars"]["compression"]["count"] == 1
    rows = [json.loads(line) for line in sidecar.read_text().splitlines() if line.strip()]
    assert rows[0]["memory_id"] == mid
    assert rows[0]["compressed_content"] == "short summary"
    assert rows[0]["compression_ratio"] == pytest.approx(0.25)


@pytest.mark.asyncio
async def test_export_bundle_from_backend_batches_sidecar_fetches_without_dropping_rows(sqlite_backend, tmp_path):
    mids = []
    for i in range(7):
        mid = await _seed_memory(sqlite_backend, content=f"batch memory {i}", namespace="default")
        mids.append(mid)
        await _seed_kg_triple(
            sqlite_backend,
            subject=f"S{i}",
            predicate="relates_to",
            obj=f"O{i}",
            memory_id=mid,
        )
        await _seed_memory_version(
            sqlite_backend,
            memory_id=mid,
            version_num=1,
            content=f"version {i}",
        )
        await _seed_compressed_variant(
            sqlite_backend,
            memory_id=mid,
            compressed_content=f"compressed {i}",
        )

    out_dir = tmp_path / "bundle"
    manifest = await charon.export_bundle_from_backend(
        sqlite_backend,
        out_dir,
        include_sidecars=True,
        sidecar_batch_size=2,
    )

    assert manifest["sidecars"]["kg_triples"]["count"] == len(mids)
    assert manifest["sidecars"]["memory_versions"]["count"] == len(mids)
    assert manifest["sidecars"]["compression"]["count"] == len(mids)
    for filename in (
        charon.KG_TRIPLES_SIDECAR,
        charon.MEMORY_VERSIONS_SIDECAR,
        charon.COMPRESSION_SIDECAR,
    ):
        path = out_dir / charon.SIDECAR_DIR / filename
        rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        assert {row["memory_id"] for row in rows} == set(mids)


# ── import_bundle_to_backend: happy path ────────────────────────────────────


@pytest.mark.asyncio
async def test_import_bundle_to_backend_inserts_memories(sqlite_backend, tmp_path):
    """A bundle written by export_bundle_from_backend re-imports with the
    same ids and concept-level content."""
    a = await _seed_memory(sqlite_backend, content="MNEMOS adopts MIF.", namespace="default")
    b = await _seed_memory(
        sqlite_backend, content="Restart gateway.", category="rules", namespace="default"
    )

    out_dir = tmp_path / "bundle"
    await charon.export_bundle_from_backend(sqlite_backend, out_dir, include_sidecars=True)

    # Fresh backend, then import.
    from mnemos.persistence.sqlite import SqliteBackend

    dst = SqliteBackend(tmp_path / "dst.sqlite3", SimpleNamespace())
    await dst.open()
    try:
        report = await charon.import_bundle_to_backend(dst, out_dir)
        assert report["memories_inserted"] == 2
        assert report["memories_skipped"] == 0

        # Round-trip preserves ids (deterministic MIF @id → mem_… mapping).
        row_a = await _get_memory(dst, a)
        row_b = await _get_memory(dst, b)
        assert row_a is not None and row_a["content"] == "MNEMOS adopts MIF."
        assert row_b is not None and row_b["content"] == "Restart gateway."
    finally:
        await dst.close()


@pytest.mark.asyncio
async def test_import_bundle_to_backend_preserves_supported_full_memory_fields(sqlite_backend, tmp_path):
    created = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    updated = datetime(2026, 1, 2, 4, 5, 6, tzinfo=timezone.utc)
    mid = await _seed_memory(
        sqlite_backend,
        content="full memory row",
        category="decisions",
        subcategory="architecture",
        metadata={"nested": {"answer": 42}, "tags": ["portable"]},
        quality_rating=97,
        owner_id="alice",
        namespace="default",
        permission_mode=640,
        source_model="gpt-4.1",
        source_provider="openai",
        source_session="sess-123",
        source_agent="agent-a",
        verbatim_content="verbatim full memory row",
        created=created,
        updated=updated,
    )
    await _update_memory_fields(sqlite_backend, mid, group_id="team-a", updated=updated)

    out_dir = tmp_path / "bundle"
    await charon.export_bundle_from_backend(sqlite_backend, out_dir, include_sidecars=True)

    from mnemos.persistence.sqlite import SqliteBackend

    dst = SqliteBackend(tmp_path / "dst.sqlite3", SimpleNamespace())
    await dst.open()
    try:
        report = await charon.import_bundle_to_backend(dst, out_dir)
        assert report["memories_inserted"] == 1

        row = await _get_memory(dst, mid)
        assert row is not None
        metadata = json.loads(row["metadata"]) if isinstance(row["metadata"], str) else row["metadata"]
        assert metadata["nested"] == {"answer": 42}
        assert row["quality_rating"] == 97
        assert row["source_provider"] == "openai"
        assert row["source_model"] == "gpt-4.1"
        assert row["source_session"] == "sess-123"
        assert row["source_agent"] == "agent-a"
        assert row["verbatim_content"] == "verbatim full memory row"
        assert row["group_id"] == "team-a"
        assert row["permission_mode"] == 640
        exported = await _get_memory_export_row(dst, mid)
        assert exported is not None
        assert datetime.fromisoformat(str(exported["created"]).replace(" ", "T")) == created
        assert datetime.fromisoformat(str(exported["updated"]).replace(" ", "T")) == updated
    finally:
        await dst.close()


@pytest.mark.asyncio
async def test_memory_sidecar_restores_embedding_and_consolidated_into(sqlite_backend, tmp_path):
    embedding = [float(i) / 1000.0 for i in range(768)]
    canonical = await _seed_memory(sqlite_backend, content="canonical memory", namespace="default")
    superseded = await _seed_memory(
        sqlite_backend,
        content="superseded memory",
        namespace="default",
        embedding=embedding,
    )
    await _update_memory_fields(sqlite_backend, superseded, consolidated_into=canonical)

    out_dir = tmp_path / "bundle"
    await charon.export_bundle_from_backend(sqlite_backend, out_dir, include_sidecars=True)

    memories_path = out_dir / charon.SIDECAR_DIR / charon.MEMORIES_SIDECAR
    sidecar_rows = [json.loads(line) for line in memories_path.read_text().splitlines() if line.strip()]
    exported = next(row for row in sidecar_rows if row["id"] == superseded)
    assert exported["consolidated_into"] == canonical
    assert exported["embedding"] == embedding

    from mnemos.persistence.sqlite import SqliteBackend

    dst = SqliteBackend(tmp_path / "dst-embedding.sqlite3", SimpleNamespace())
    await dst.open()
    try:
        report = await charon.import_bundle_to_backend(dst, out_dir)
        assert report["memories_inserted"] == 2

        row = await _get_memory_export_row(dst, superseded)
        assert row is not None
        restored_embedding = json.loads(row["embedding"]) if isinstance(row["embedding"], str) else row["embedding"]
        assert row["consolidated_into"] == canonical
        assert restored_embedding == embedding
    finally:
        await dst.close()


@pytest.mark.asyncio
async def test_import_bundle_to_backend_concept_beats_memory_sidecar_for_visible_fields(sqlite_backend, tmp_path):
    mid = await _seed_memory(
        sqlite_backend,
        content="sidecar content",
        category="facts",
        subcategory="old-taxonomy",
        namespace="default",
    )

    out_dir = tmp_path / "bundle"
    await charon.export_bundle_from_backend(sqlite_backend, out_dir, include_sidecars=True)

    concept_path = next(path for path in out_dir.rglob("*.md") if mid in path.read_text())
    concept = mif.markdown_to_concept(concept_path.read_text(encoding="utf-8"))
    concept["content"] = "operator edited concept content"
    concept["tags"] = ["decisions", "new-taxonomy"]
    concept["properties"]["mnemos:category"] = "decisions"
    concept["properties"]["mnemos:subcategory"] = "new-taxonomy"
    concept_path.write_text(mif.concept_to_markdown(concept), encoding="utf-8")

    memories_path = out_dir / charon.SIDECAR_DIR / charon.MEMORIES_SIDECAR
    sidecar_rows = [json.loads(line) for line in memories_path.read_text().splitlines() if line.strip()]
    assert next(row for row in sidecar_rows if row["id"] == mid)["content"] == "sidecar content"

    from mnemos.persistence.sqlite import SqliteBackend

    dst = SqliteBackend(tmp_path / "dst-concept-authority.sqlite3", SimpleNamespace())
    await dst.open()
    try:
        await charon.import_bundle_to_backend(dst, out_dir)
        row = await _get_memory(dst, mid)
        assert row is not None
        assert row["content"] == "operator edited concept content"
        assert row["category"] == "decisions"
        assert row["subcategory"] == "new-taxonomy"
    finally:
        await dst.close()


@pytest.mark.asyncio
async def test_import_bundle_to_backend_replays_kg_triples(sqlite_backend, tmp_path):
    mid = await _seed_memory(sqlite_backend, content="Athena", namespace="default")
    await _seed_memory(sqlite_backend, content="placeholder", namespace="default")
    t1 = await _seed_kg_triple(
        sqlite_backend, subject="Athena", predicate="guides", obj="Odysseus", memory_id=mid
    )
    t2 = await _seed_kg_triple(
        sqlite_backend, subject="Hermes", predicate="visits", obj="Ithaca", memory_id=None
    )

    out_dir = tmp_path / "bundle"
    await charon.export_bundle_from_backend(sqlite_backend, out_dir, include_sidecars=True)

    from mnemos.persistence.sqlite import SqliteBackend

    dst = SqliteBackend(tmp_path / "dst.sqlite3", SimpleNamespace())
    await dst.open()
    try:
        report = await charon.import_bundle_to_backend(dst, out_dir)
        assert report["kg_triples_inserted"] == 2

        # Both triples (attached + unattached) survive the round-trip.
        row_t1 = await _get_kg_triple(dst, t1)
        row_t2 = await _get_kg_triple(dst, t2)
        assert row_t1 is not None and row_t1["subject"] == "Athena" and row_t1["memory_id"] == mid
        assert row_t2 is not None and row_t2["subject"] == "Hermes" and row_t2["memory_id"] is None
    finally:
        await dst.close()


@pytest.mark.asyncio
async def test_import_bundle_to_backend_preserve_ids_false_remaps_sidecar_memory_refs(sqlite_backend, tmp_path):
    mid = await _seed_memory(sqlite_backend, content="fresh-id source", namespace="default")
    triple_id = await _seed_kg_triple(
        sqlite_backend,
        subject="Athena",
        predicate="guides",
        obj="Odysseus",
        memory_id=mid,
    )
    version_id = await _seed_memory_version(
        sqlite_backend,
        memory_id=mid,
        version_num=1,
        content="fresh version",
    )
    merge_parent_id = await _seed_memory_version(
        sqlite_backend,
        memory_id=mid,
        version_num=2,
        content="fresh merged version",
        parent_version_id=version_id,
        merge_parents=[version_id],
    )
    await _seed_compressed_variant(
        sqlite_backend,
        memory_id=mid,
        compressed_content="fresh compressed",
    )

    out_dir = tmp_path / "bundle"
    await charon.export_bundle_from_backend(sqlite_backend, out_dir, include_sidecars=True)

    from mnemos.persistence.sqlite import SqliteBackend
    from mnemos.persistence.visibility import VisibilityFilter, VisibilityScope

    dst = SqliteBackend(tmp_path / "dst.sqlite3", SimpleNamespace())
    await dst.open()
    try:
        await _seed_memory(dst, memory_id=mid, content="preseed original", namespace="default")
        await _seed_kg_triple(
            dst,
            triple_id=triple_id,
            subject="Athena",
            predicate="guides",
            obj="Odysseus",
            memory_id=mid,
        )
        await _seed_memory_version(
            dst,
            version_id=version_id,
            memory_id=mid,
            version_num=1,
            content="preseed original version",
        )
        await _seed_memory_version(
            dst,
            version_id=merge_parent_id,
            memory_id=mid,
            version_num=2,
            content="preseed original merged version",
            parent_version_id=version_id,
            merge_parents=[version_id],
        )
        await _seed_compressed_variant(
            dst,
            memory_id=mid,
            compressed_content="preseed original compressed",
        )

        report = await charon.import_bundle_to_backend(dst, out_dir, preserve_ids=False)
        assert report["memories_inserted"] == 1

        vis = VisibilityFilter(
            scope=VisibilityScope.ROOT_BYPASS,
            user_id=None,
            group_ids=(),
            namespace=None,
            exclude_namespaces=(),
        )
        async with dst.transactional() as tx:
            rows, _ = await dst.memories.list_memories(tx, visibility=vis, include_archived=True)
        assert len(rows) == 2
        new_mid = next(row["id"] for row in rows if row["id"] != mid)
        assert new_mid != mid

        async with dst.transactional() as tx:
            kg_rows = await dst.kg_triples.fetch_kg_triples_for_export(
                tx,
                memory_ids=[new_mid],
                effective_owner="alice",
                effective_ns="default",
                include_unattached=False,
                hard_limit=10,
            )
            version_rows = await dst.memory_versions.fetch_memory_versions_for_export(
                tx,
                memory_ids=[new_mid],
                effective_owner="alice",
                effective_ns="default",
                hard_limit=10,
            )
        compression_row = await _get_compressed_variant(dst, new_mid)
        assert len(kg_rows) == 1
        assert kg_rows[0]["id"] != triple_id
        assert kg_rows[0]["memory_id"] == new_mid
        assert len(version_rows) == 2
        versions_by_num = {row["version_num"]: row for row in version_rows}
        assert versions_by_num[1]["id"] != version_id
        assert versions_by_num[2]["id"] != merge_parent_id
        assert versions_by_num[1]["memory_id"] == new_mid
        assert versions_by_num[2]["memory_id"] == new_mid
        assert versions_by_num[2]["parent_version_id"] == versions_by_num[1]["id"]
        merge_parents = versions_by_num[2]["merge_parents"]
        if isinstance(merge_parents, str):
            merge_parents = json.loads(merge_parents)
        assert merge_parents == [versions_by_num[1]["id"]]
        assert compression_row is not None and compression_row["compressed_content"] == "fresh compressed"
        assert (await _get_compressed_variant(dst, mid))["compressed_content"] == "preseed original compressed"
    finally:
        await dst.close()


@pytest.mark.asyncio
async def test_import_bundle_to_backend_remaps_non_uuid_sidecar_ids_for_uuid_repos(sqlite_backend, tmp_path):
    mid = await _seed_memory(sqlite_backend, memory_id="mem_uuid_target", content="uuid target")
    kg_id = await _seed_kg_triple(
        sqlite_backend,
        triple_id="kg_string_target",
        subject="Athena",
        predicate="guides",
        obj="Odysseus",
        memory_id=mid,
    )
    root_version_id = await _seed_memory_version(
        sqlite_backend,
        version_id="v_root",
        memory_id=mid,
        version_num=1,
        content="root version",
    )
    child_version_id = await _seed_memory_version(
        sqlite_backend,
        version_id="v_child",
        memory_id=mid,
        version_num=2,
        content="child version",
        parent_version_id=root_version_id,
        merge_parents=[root_version_id],
    )

    out_dir = tmp_path / "bundle"
    await charon.export_bundle_from_backend(sqlite_backend, out_dir, include_sidecars=True)

    from mnemos.persistence.sqlite import SqliteBackend

    dst = SqliteBackend(tmp_path / "dst_uuid_required.sqlite3", SimpleNamespace())
    await dst.open()
    try:
        dst.kg_triples.requires_uuid_ids = True
        dst.memory_versions.requires_uuid_ids = True

        report = await charon.import_bundle_to_backend(dst, out_dir)
        assert report["kg_triples_inserted"] == 1
        assert report["memory_versions_inserted"] == 2

        async with dst.transactional() as tx:
            kg_rows = await dst.kg_triples.fetch_kg_triples_for_export(
                tx,
                memory_ids=[mid],
                effective_owner="alice",
                effective_ns="default",
                include_unattached=False,
                hard_limit=10,
            )
            version_rows = await dst.memory_versions.fetch_memory_versions_for_export(
                tx,
                memory_ids=[mid],
                effective_owner="alice",
                effective_ns="default",
                hard_limit=10,
            )

        assert len(kg_rows) == 1
        remapped_kg_id = kg_rows[0]["id"]
        assert remapped_kg_id != kg_id
        uuid.UUID(remapped_kg_id)

        assert len(version_rows) == 2
        versions_by_num = {row["version_num"]: row for row in version_rows}
        remapped_root_id = versions_by_num[1]["id"]
        remapped_child_id = versions_by_num[2]["id"]
        assert remapped_root_id != root_version_id
        assert remapped_child_id != child_version_id
        uuid.UUID(remapped_root_id)
        uuid.UUID(remapped_child_id)
        assert versions_by_num[2]["parent_version_id"] == remapped_root_id
        merge_parents = versions_by_num[2]["merge_parents"]
        if isinstance(merge_parents, str):
            merge_parents = json.loads(merge_parents)
        assert merge_parents == [remapped_root_id]
    finally:
        await dst.close()


@pytest.mark.asyncio
async def test_import_bundle_to_backend_replays_memory_versions(sqlite_backend, tmp_path):
    mid = await _seed_memory(sqlite_backend, content="a memory", namespace="default")
    v1 = await _seed_memory_version(
        sqlite_backend, memory_id=mid, version_num=1, content="first"
    )
    v2 = await _seed_memory_version(
        sqlite_backend,
        memory_id=mid,
        version_num=2,
        content="second",
        parent_version_id=v1,
    )

    out_dir = tmp_path / "bundle"
    await charon.export_bundle_from_backend(sqlite_backend, out_dir, include_sidecars=True)

    from mnemos.persistence.sqlite import SqliteBackend

    dst = SqliteBackend(tmp_path / "dst.sqlite3", SimpleNamespace())
    await dst.open()
    try:
        report = await charon.import_bundle_to_backend(dst, out_dir)
        assert report["memory_versions_inserted"] == 2

        row_v1 = await _get_memory_version(dst, v1)
        row_v2 = await _get_memory_version(dst, v2)
        assert row_v1 is not None and row_v1["content"] == "first"
        assert row_v2 is not None and row_v2["content"] == "second"
        assert row_v2["parent_version_id"] == v1
    finally:
        await dst.close()


@pytest.mark.asyncio
async def test_import_bundle_to_backend_rebuilds_branch_heads_for_replayed_versions(sqlite_backend, tmp_path):
    mid = await _seed_memory(sqlite_backend, content="a memory", namespace="default")
    v1 = await _seed_memory_version(
        sqlite_backend, memory_id=mid, version_num=1, content="first"
    )
    v2 = await _seed_memory_version(
        sqlite_backend,
        memory_id=mid,
        version_num=2,
        content="second",
        parent_version_id=v1,
    )

    out_dir = tmp_path / "bundle"
    await charon.export_bundle_from_backend(sqlite_backend, out_dir, include_sidecars=True)

    from mnemos.persistence.sqlite import SqliteBackend

    dst = SqliteBackend(tmp_path / "dst-branch-heads.sqlite3", SimpleNamespace())
    await dst.open()
    try:
        await charon.import_bundle_to_backend(dst, out_dir)

        row_v2 = await _get_memory_version(dst, v2)
        assert row_v2 is not None
        log = await _get_memory_log(dst, mid)
        assert [row["version_num"] for row in log] == [2, 1]
        assert log[0]["commit_hash"] == row_v2["commit_hash"]
    finally:
        await dst.close()


@pytest.mark.asyncio
async def test_import_bundle_to_backend_does_not_move_existing_branch_head_backward(sqlite_backend, tmp_path):
    mid = await _seed_memory(sqlite_backend, content="a memory", namespace="default")
    v1 = await _seed_memory_version(
        sqlite_backend, memory_id=mid, version_num=1, content="first"
    )
    v2 = await _seed_memory_version(
        sqlite_backend,
        memory_id=mid,
        version_num=2,
        content="second",
        parent_version_id=v1,
    )

    old_bundle = tmp_path / "old-bundle"
    await charon.export_bundle_from_backend(sqlite_backend, old_bundle, include_sidecars=True)

    v3 = await _seed_memory_version(
        sqlite_backend,
        memory_id=mid,
        version_num=3,
        content="third",
        parent_version_id=v2,
    )
    v4 = await _seed_memory_version(
        sqlite_backend,
        memory_id=mid,
        version_num=4,
        content="fourth",
        parent_version_id=v3,
    )

    new_bundle = tmp_path / "new-bundle"
    await charon.export_bundle_from_backend(sqlite_backend, new_bundle, include_sidecars=True)

    from mnemos.persistence.sqlite import SqliteBackend

    dst = SqliteBackend(tmp_path / "dst-branch-regression.sqlite3", SimpleNamespace())
    await dst.open()
    try:
        await _seed_memory(dst, memory_id=mid, content="a memory", namespace="default")
        await _seed_memory_version(
            dst, version_id=v1, memory_id=mid, version_num=1, content="first"
        )
        await _seed_memory_version(
            dst,
            version_id=v2,
            memory_id=mid,
            version_num=2,
            content="second",
            parent_version_id=v1,
        )
        await _seed_memory_version(
            dst,
            version_id=v3,
            memory_id=mid,
            version_num=3,
            content="third already local",
            parent_version_id=v2,
        )
        async with dst.transactional() as tx:
            await dst.memory_branches.upsert_memory_branch_head(
                tx,
                memory_id=mid,
                branch="main",
                head_version_id=v3,
            )

        log = await _get_memory_log(dst, mid)
        assert [row["version_num"] for row in log] == [3, 2, 1]

        report = await charon.import_bundle_to_backend(dst, old_bundle)
        assert report["memory_versions_inserted"] == 2
        log = await _get_memory_log(dst, mid)
        assert [row["version_num"] for row in log] == [3, 2, 1]

        report = await charon.import_bundle_to_backend(dst, new_bundle)
        assert report["memory_versions_inserted"] == 4
        row_v4 = await _get_memory_version(dst, v4)
        assert row_v4 is not None
        log = await _get_memory_log(dst, mid)
        assert [row["version_num"] for row in log] == [4, 3, 2, 1]
    finally:
        await dst.close()


@pytest.mark.asyncio
async def test_import_bundle_to_backend_replays_compression(sqlite_backend, tmp_path):
    mid = await _seed_memory(sqlite_backend, content="a memory", namespace="default")
    await _seed_compressed_variant(
        sqlite_backend,
        memory_id=mid,
        compressed_content="short summary",
        compression_ratio=0.25,
        quality_score=0.92,
    )

    out_dir = tmp_path / "bundle"
    await charon.export_bundle_from_backend(sqlite_backend, out_dir, include_sidecars=True)

    from mnemos.persistence.sqlite import SqliteBackend

    dst = SqliteBackend(tmp_path / "dst.sqlite3", SimpleNamespace())
    await dst.open()
    try:
        report = await charon.import_bundle_to_backend(dst, out_dir)
        assert report["compressed_variants_inserted"] == 1
        row = await _get_compressed_variant(dst, mid)
        assert row is not None and row["compressed_content"] == "short summary"
        assert row["compression_ratio"] == pytest.approx(0.25)
    finally:
        await dst.close()


# ── round-trip property ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_full_round_trip_memories_kg_versions_compression(sqlite_backend, tmp_path):
    """export → import reproduces the full set: memories + kg triples +
    memory versions + compression variants."""
    mid_a = await _seed_memory(sqlite_backend, content="MNEMOS adopts MIF.", namespace="default")
    mid_b = await _seed_memory(
        sqlite_backend, content="Restart gateway.", category="rules", namespace="default"
    )

    t1 = await _seed_kg_triple(
        sqlite_backend, subject="Athena", predicate="guides", obj="Odysseus", memory_id=mid_a
    )
    t2 = await _seed_kg_triple(
        sqlite_backend, subject="Hermes", predicate="visits", obj="Ithaca", memory_id=None
    )

    v1 = await _seed_memory_version(
        sqlite_backend, memory_id=mid_a, version_num=1, content="first"
    )
    v2 = await _seed_memory_version(
        sqlite_backend,
        memory_id=mid_a,
        version_num=2,
        content="second",
        parent_version_id=v1,
    )

    await _seed_compressed_variant(
        sqlite_backend, memory_id=mid_a, compressed_content="compressed A"
    )
    await _seed_compressed_variant(
        sqlite_backend, memory_id=mid_b, compressed_content="compressed B"
    )

    out_dir = tmp_path / "bundle"
    manifest = await charon.export_bundle_from_backend(sqlite_backend, out_dir)
    assert manifest["count"] == 2
    assert manifest["sidecars"]["kg_triples"]["count"] == 2
    assert manifest["sidecars"]["memory_versions"]["count"] == 2
    assert manifest["sidecars"]["compression"]["count"] == 2

    from mnemos.persistence.sqlite import SqliteBackend

    dst = SqliteBackend(tmp_path / "dst.sqlite3", SimpleNamespace())
    await dst.open()
    try:
        report = await charon.import_bundle_to_backend(dst, out_dir)
        assert report["memories_inserted"] == 2
        assert report["kg_triples_inserted"] == 2
        assert report["memory_versions_inserted"] == 2
        assert report["compressed_variants_inserted"] == 2

        # Spot-check: every seeded id reproduces on the destination.
        for mid in (mid_a, mid_b):
            row = await _get_memory(dst, mid)
            assert row is not None
        for tid in (t1, t2):
            assert await _get_kg_triple(dst, tid) is not None
        for vid in (v1, v2):
            assert await _get_memory_version(dst, vid) is not None
        for mid in (mid_a, mid_b):
            assert await _get_compressed_variant(dst, mid) is not None

        # v2's parent link survives.
        v2_row = await _get_memory_version(dst, v2)
        assert v2_row["parent_version_id"] == v1
    finally:
        await dst.close()


@pytest.mark.asyncio
async def test_import_is_idempotent_when_bundle_reapplied(sqlite_backend, tmp_path):
    """Re-importing the same bundle into a backend that already has the
    rows is a no-op, not a failure (CHARON must be re-apply safe)."""
    mid = await _seed_memory(sqlite_backend, content="MNEMOS.", namespace="default")

    out_dir = tmp_path / "bundle"
    await charon.export_bundle_from_backend(sqlite_backend, out_dir, include_sidecars=True)

    from mnemos.persistence.sqlite import SqliteBackend

    dst = SqliteBackend(tmp_path / "dst.sqlite3", SimpleNamespace())
    await dst.open()
    try:
        first = await charon.import_bundle_to_backend(dst, out_dir)
        assert first["memories_inserted"] == 1

        second = await charon.import_bundle_to_backend(dst, out_dir)
        # Memories: skipped (DuplicateMemoryError), KG / versions /
        # compression: re-applied (they're idempotent on UNIQUE collisions
        # via ``INSERT OR IGNORE`` semantics in the backend, so the second
        # pass is harmless).
        assert second["memories_inserted"] == 0
        assert second["memories_skipped"] == 1

        # The destination still has exactly one row for ``mid``.
        row = await _get_memory(dst, mid)
        assert row is not None
    finally:
        await dst.close()


@pytest.mark.asyncio
async def test_export_without_sidecars_drops_the_sidecar_block(sqlite_backend, tmp_path):
    mid = await _seed_memory(sqlite_backend, content="a memory", namespace="default")
    await _seed_kg_triple(
        sqlite_backend, subject="Athena", predicate="guides", obj="Odysseus", memory_id=mid
    )

    out_dir = tmp_path / "bundle"
    manifest = await charon.export_bundle_from_backend(
        sqlite_backend, out_dir, include_sidecars=False
    )

    assert manifest["sidecars_included"] is False
    assert "sidecars" not in manifest
    # And the sidecar directory was never created.
    assert not (out_dir / charon.SIDECAR_DIR).exists()


# ── import hardening regressions ────────────────────────────────────────────


class _CaptureTx:
    async def commit(self):
        return None

    async def rollback(self):
        return None


class _CaptureTxContext:
    async def __aenter__(self):
        return _CaptureTx()

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _CaptureMemories:
    def __init__(self):
        self.inserted = []
        self.updated = []
        self.suppress_snapshot_calls = 0

    async def set_suppress_version_snapshot(self, tx):
        self.suppress_snapshot_calls += 1

    async def insert_memory(self, tx, **kwargs):
        self.inserted.append(kwargs)
        return "INSERT 0 1"

    async def update_memory(self, tx, memory_id, *, visibility, fields):
        self.updated.append({"memory_id": memory_id, "fields": fields})
        return {"id": memory_id, **fields}


class _CaptureKG:
    def __init__(self):
        self.inserted = []

    async def insert_kg_triple(self, tx, **kwargs):
        self.inserted.append(kwargs)
        return "INSERT 0 1"


class _CaptureVersions:
    def __init__(self):
        self.inserted = []

    async def insert_memory_version(self, tx, **kwargs):
        self.inserted.append(kwargs)
        return "INSERT 0 1"


class _CaptureBranches:
    def __init__(self, versions: _CaptureVersions):
        self._versions = versions
        self.upserted = []

    async def fetch_memory_branch_heads(self, tx, memory_ids, *, authorized_version_uuids=None):
        allowed = set(authorized_version_uuids) if authorized_version_uuids is not None else None
        heads = {}
        for row in self._versions.inserted:
            version_id = row.get("version_id")
            memory_id = row.get("memory_id")
            if memory_id not in memory_ids:
                continue
            if allowed is not None and version_id not in allowed:
                continue
            branch = row.get("branch") or "main"
            key = (memory_id, branch)
            current = heads.get(key)
            if current is None or int(row.get("version_num") or 0) > int(current.get("version_num") or 0):
                heads[key] = row
        return [
            {
                "memory_id": memory_id,
                "branch": branch,
                "head_version_id": row["version_id"],
            }
            for (memory_id, branch), row in heads.items()
        ]

    async def upsert_memory_branch_head(self, tx, **kwargs):
        self.upserted.append(kwargs)


class _CaptureCompression:
    def __init__(self):
        self.inserted = []

    async def insert_compressed_variant(self, tx, **kwargs):
        self.inserted.append(kwargs)
        return "INSERT 0 1"


class _CaptureBackend:
    def __init__(self):
        self.memories = _CaptureMemories()
        self.kg_triples = _CaptureKG()
        self.memory_versions = _CaptureVersions()
        self.memory_branches = _CaptureBranches(self.memory_versions)
        self.compression = _CaptureCompression()

    def transactional(self):
        return _CaptureTxContext()


class _AutoSnapshotVersions(_CaptureVersions):
    def auto_snapshot(self, memory_id: str):
        self.inserted.append(
            {
                "version_id": "auto-v1",
                "memory_id": memory_id,
                "version_num": 1,
                "branch": "main",
                "synthetic": True,
            }
        )

    async def insert_memory_version(self, tx, **kwargs):
        branch = kwargs.get("branch") or "main"
        version_num = int(kwargs.get("version_num") or 1)
        for existing in self.inserted:
            if (
                existing.get("memory_id") == kwargs.get("memory_id")
                and int(existing.get("version_num") or 1) == version_num
                and (existing.get("branch") or "main") == branch
                and existing.get("version_id") != kwargs.get("version_id")
            ):
                raise AssertionError("duplicate memory_versions natural key")
        return await super().insert_memory_version(tx, **kwargs)


class _AutoSnapshotMemories(_CaptureMemories):
    def __init__(self, versions: _AutoSnapshotVersions):
        super().__init__()
        self._versions = versions

    async def insert_memory(self, tx, **kwargs):
        result = await super().insert_memory(tx, **kwargs)
        if not self.suppress_snapshot_calls:
            self._versions.auto_snapshot(kwargs["memory_id"])
        return result


class _AutoSnapshotBackend(_CaptureBackend):
    def __init__(self):
        self.memory_versions = _AutoSnapshotVersions()
        self.memories = _AutoSnapshotMemories(self.memory_versions)
        self.kg_triples = _CaptureKG()
        self.memory_branches = _CaptureBranches(self.memory_versions)
        self.compression = _CaptureCompression()


def _write_bundle_sidecar(bundle, name: str, filename: str, rows: list[dict], *, count: int | None = None):
    sidecar_dir = bundle / charon.SIDECAR_DIR
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    path = sidecar_dir / filename
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    manifest_path = bundle / charon.MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text())
    manifest.setdefault("sidecars", {})[name] = {
        "path": f"{charon.SIDECAR_DIR}/{filename}",
        "count": len(rows) if count is None else count,
        "truncated": False,
    }
    manifest["sidecars_included"] = True
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


@pytest.mark.asyncio
async def test_import_bundle_to_backend_suppresses_auto_snapshot_when_replaying_versions(tmp_path):
    charon.export_bundle(
        [
            {
                "id": "mem_auto_snapshot",
                "content": "sidecar history is authoritative",
                "category": "facts",
                "namespace": "default",
                "owner_id": "alice",
            }
        ],
        tmp_path,
    )
    _write_bundle_sidecar(
        tmp_path,
        "memory_versions",
        charon.MEMORY_VERSIONS_SIDECAR,
        [
            {
                "id": "v_exported_1",
                "memory_id": "mem_auto_snapshot",
                "version_num": 1,
                "content": "exported first",
                "owner_id": "alice",
                "namespace": "default",
                "branch": "main",
                "merge_parents": [],
            },
            {
                "id": "v_exported_2",
                "memory_id": "mem_auto_snapshot",
                "version_num": 2,
                "content": "exported second",
                "owner_id": "alice",
                "namespace": "default",
                "branch": "main",
                "parent_version_id": "v_exported_1",
                "merge_parents": [],
            },
        ],
    )

    backend = _AutoSnapshotBackend()
    report = await charon.import_bundle_to_backend(backend, tmp_path)

    assert backend.memories.suppress_snapshot_calls == 1
    assert report["memory_versions_inserted"] == 2
    assert [row["version_id"] for row in backend.memory_versions.inserted] == [
        "v_exported_1",
        "v_exported_2",
    ]
    assert not any(row.get("synthetic") for row in backend.memory_versions.inserted)
    assert backend.memory_branches.upserted == [
        {
            "memory_id": "mem_auto_snapshot",
            "branch": "main",
            "head_version_id": "v_exported_2",
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("manifest_mode", ["undeclared", "included_false"])
async def test_import_bundle_to_backend_ignores_sidecars_not_declared_by_manifest(tmp_path, manifest_mode):
    charon.export_bundle(
        [
            {
                "id": "mem_manifest_authority",
                "content": "manifest authority",
                "category": "facts",
                "namespace": "default",
                "owner_id": "alice",
            }
        ],
        tmp_path,
    )
    sidecar_dir = tmp_path / charon.SIDECAR_DIR
    sidecar_dir.mkdir(parents=True)
    raw_sidecars = {
        charon.KG_TRIPLES_SIDECAR: [
            {
                "id": "kg_undeclared",
                "subject": "A",
                "predicate": "related_to",
                "object": "B",
                "memory_id": "mem_manifest_authority",
                "owner_id": "alice",
                "namespace": "default",
            }
        ],
        charon.MEMORY_VERSIONS_SIDECAR: [
            {
                "id": "v_undeclared",
                "memory_id": "mem_manifest_authority",
                "version_num": 1,
                "content": "undeclared version",
                "owner_id": "alice",
                "namespace": "default",
                "merge_parents": [],
            }
        ],
        charon.COMPRESSION_SIDECAR: [
            {
                "memory_id": "mem_manifest_authority",
                "owner_id": "alice",
                "engine_id": "engine",
            }
        ],
    }
    for filename, rows in raw_sidecars.items():
        (sidecar_dir / filename).write_text(
            "".join(json.dumps(row) + "\n" for row in rows),
            encoding="utf-8",
        )

    manifest_path = tmp_path / charon.MANIFEST_NAME
    if manifest_mode == "included_false":
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["sidecars_included"] = False
        manifest["sidecars"] = {
            "kg_triples": {
                "path": f"{charon.SIDECAR_DIR}/{charon.KG_TRIPLES_SIDECAR}",
                "count": 1,
                "truncated": False,
            },
            "memory_versions": {
                "path": f"{charon.SIDECAR_DIR}/{charon.MEMORY_VERSIONS_SIDECAR}",
                "count": 1,
                "truncated": False,
            },
            "compression": {
                "path": f"{charon.SIDECAR_DIR}/{charon.COMPRESSION_SIDECAR}",
                "count": 1,
                "truncated": False,
            },
        }
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    backend = _CaptureBackend()
    report = await charon.import_bundle_to_backend(backend, tmp_path)

    assert report["memories_inserted"] == 1
    assert report["kg_triples_inserted"] == 0
    assert report["memory_versions_inserted"] == 0
    assert report["compressed_variants_inserted"] == 0
    assert backend.kg_triples.inserted == []
    assert backend.memory_versions.inserted == []
    assert backend.compression.inserted == []


@pytest.mark.asyncio
async def test_import_bundle_to_backend_parses_timestamps_and_keeps_merge_parents_as_list(tmp_path):
    charon.export_bundle(
        [
            {
                "id": "mem_ts",
                "content": "timestamped memory",
                "category": "facts",
                "namespace": "default",
                "owner_id": "alice",
                "created": "2026-01-02T03:04:05Z",
                "updated": "2026-01-02T04:05:06Z",
            }
        ],
        tmp_path,
    )
    _write_bundle_sidecar(
        tmp_path,
        "kg_triples",
        charon.KG_TRIPLES_SIDECAR,
        [
            {
                "id": "kg_ts",
                "subject": "A",
                "predicate": "related_to",
                "object": "B",
                "memory_id": "mem_ts",
                "valid_from": "2026-01-02T05:06:07Z",
                "valid_until": "2026-01-03T05:06:07Z",
                "created": "2026-01-02T05:06:08Z",
                "owner_id": "alice",
                "namespace": "default",
            }
        ],
    )
    _write_bundle_sidecar(
        tmp_path,
        "memory_versions",
        charon.MEMORY_VERSIONS_SIDECAR,
        [
            {
                "id": "11111111-1111-1111-1111-111111111111",
                "memory_id": "mem_ts",
                "version_num": 1,
                "content": "timestamped version",
                "metadata": {"a": 1},
                "owner_id": "alice",
                "namespace": "default",
                "snapshot_at": "2026-01-02T06:07:08Z",
                "parent_version_id": "22222222-2222-2222-2222-222222222222",
                "merge_parents": ["33333333-3333-3333-3333-333333333333"],
            }
        ],
    )
    _write_bundle_sidecar(
        tmp_path,
        "compression",
        charon.COMPRESSION_SIDECAR,
        [
            {
                "memory_id": "mem_ts",
                "owner_id": "alice",
                "engine_id": "engine",
                "selected_at": "2026-01-02T07:08:09Z",
            }
        ],
    )

    backend = _CaptureBackend()
    await charon.import_bundle_to_backend(backend, tmp_path)

    assert isinstance(backend.memories.inserted[0]["created"], datetime)
    assert backend.memories.inserted[0]["created"].tzinfo is not None
    assert isinstance(backend.kg_triples.inserted[0]["valid_from"], datetime)
    assert isinstance(backend.memory_versions.inserted[0]["snapshot_at"], datetime)
    assert isinstance(backend.compression.inserted[0]["selected_at"], datetime)
    assert backend.memory_versions.inserted[0]["merge_parents"] == [
        "33333333-3333-3333-3333-333333333333"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["missing", "short"])
async def test_import_bundle_to_backend_validates_declared_sidecar_files_and_counts(tmp_path, damage):
    charon.export_bundle(
        [
            {
                "id": "mem_damage",
                "content": "damaged bundle memory",
                "category": "facts",
                "namespace": "default",
                "owner_id": "alice",
            }
        ],
        tmp_path,
    )
    sidecar = _write_bundle_sidecar(
        tmp_path,
        "kg_triples",
        charon.KG_TRIPLES_SIDECAR,
        [
            {
                "id": "kg_damage",
                "subject": "A",
                "predicate": "related_to",
                "object": "B",
                "memory_id": "mem_damage",
                "owner_id": "alice",
                "namespace": "default",
            }
        ],
        count=2 if damage == "short" else 1,
    )
    if damage == "missing":
        sidecar.unlink()

    with pytest.raises(ValueError, match="kg_triples"):
        await charon.import_bundle_to_backend(_CaptureBackend(), tmp_path)


def test_import_bundle_rejects_manifest_concept_path_traversal(tmp_path):
    manifest = {
        "mif_version": "1.0.0",
        "schema": "https://mif-spec.dev/schema/mif.schema.json",
        "concepts": [{"path": "../escape.md"}],
    }
    (tmp_path / charon.MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="escapes root"):
        charon.import_bundle(tmp_path)


@pytest.mark.asyncio
async def test_import_bundle_to_backend_rejects_manifest_sidecar_path_traversal(tmp_path):
    charon.export_bundle(
        [
            {
                "id": "mem_sidecar_escape",
                "content": "sidecar escape",
                "category": "facts",
                "namespace": "default",
            }
        ],
        tmp_path,
    )
    manifest_path = tmp_path / charon.MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text())
    manifest["sidecars"] = {
        "kg_triples": {
            "path": "../kg_triples.jsonl",
            "count": 1,
            "truncated": False,
        }
    }
    manifest["sidecars_included"] = True
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="escapes root"):
        await charon.import_bundle_to_backend(_CaptureBackend(), tmp_path)


def test_coerce_embedding_accepts_array_array():
    # python-oracledb returns VECTOR columns as array.array('f', ...); CHARON must
    # coerce it to list[float] (not None) so Oracle->Oracle export keeps embeddings.
    import array as _arr

    vec = _arr.array("f", [0.1, 0.2, 0.3])
    out = charon._coerce_embedding(vec)
    assert out == pytest.approx([0.1, 0.2, 0.3], rel=1e-6)
    # JSON-serializable for the sidecar
    assert json.loads(json.dumps(out)) == pytest.approx([0.1, 0.2, 0.3], rel=1e-6)
    # existing string / list paths still work; junk -> None
    assert charon._coerce_embedding("[1, 2, 3]") == [1.0, 2.0, 3.0]
    assert charon._coerce_embedding([1, 2]) == [1.0, 2.0]
    assert charon._coerce_embedding(None) is None
    assert charon._coerce_embedding("not-a-vector") is None
    assert charon._coerce_embedding({"a": 1}) is None


@pytest.mark.asyncio
async def test_export_bundle_from_backend_excludes_soft_deleted(sqlite_backend, tmp_path):
    """Soft-deleted memories must NOT be exported (no resurrection on re-import)."""
    from mnemos.persistence.visibility import VisibilityFilter, VisibilityScope

    await _seed_memory(sqlite_backend, content="live memory keep", namespace="default")
    gone = await _seed_memory(sqlite_backend, content="tombstoned memory drop", namespace="default")
    vis = VisibilityFilter(
        scope=VisibilityScope.ROOT_BYPASS, user_id=None, group_ids=(), namespace=None, exclude_namespaces=()
    )
    async with sqlite_backend.transactional() as tx:
        await sqlite_backend.memories.soft_delete_memory(tx, gone, visibility=vis)

    out_dir = tmp_path / "bundle"
    manifest = await charon.export_bundle_from_backend(sqlite_backend, out_dir, include_sidecars=False)

    blob = "\n".join(p.read_text() for p in out_dir.rglob("*.md"))
    assert "live memory keep" in blob
    assert "tombstoned memory drop" not in blob
    assert manifest["count"] == 1
