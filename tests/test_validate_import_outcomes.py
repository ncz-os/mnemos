from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mnemos.portability import charon


@pytest.mark.asyncio
@pytest.mark.parametrize("duplicate_exception", [False, True])
async def test_import_skips_existing_rows_and_sidecars(tmp_path, monkeypatch, duplicate_exception):
    rows = [
        dict(id=mid, content="note", category="facts", owner_id="alice", namespace="notes", group_id="group")
        for mid in ["existing", "new"]
    ]
    monkeypatch.setattr(charon, "import_bundle", lambda src: rows)
    monkeypatch.setattr(charon, "_declared_sidecars", lambda manifest: {"memories": "rows", "kg_triples": "kg"})

    def sidecars(src, declared, name, filename):
        if name == "memories":
            return rows
        if name == "kg_triples":
            return [dict(id=mid, memory_id=mid, subject="a", predicate="b", object="c") for mid in ["existing", "new"]]
        return []

    monkeypatch.setattr(charon, "_sidecar_rows", sidecars)
    monkeypatch.setattr(charon, "assert_no_active_deletion_for_scopes", AsyncMock())

    async def insert(tx, **kw):
        if kw["memory_id"] == "existing":
            if duplicate_exception:
                from mnemos.persistence.sqlite import DuplicateMemoryError

                raise DuplicateMemoryError("existing")
            return "INSERT 0 0"
        return "INSERT 0 1"

    @asynccontextmanager
    async def transactional():
        yield object()

    backend = SimpleNamespace(
        transactional=transactional,
        memories=SimpleNamespace(insert_memory=insert, update_memory=AsyncMock()),
        kg_triples=SimpleNamespace(insert_kg_triple=AsyncMock()),
        memory_versions=SimpleNamespace(),
        compression=SimpleNamespace(),
    )
    report = await charon.import_bundle_to_backend(backend, tmp_path)
    assert report["memories_inserted"] == 1 and report["memories_skipped"] == 1
    assert [call.args[1] for call in backend.memories.update_memory.await_args_list] == ["new"]
    assert [call.kwargs["memory_id"] for call in backend.kg_triples.insert_kg_triple.await_args_list] == ["new"]
