"""A storage outage must degrade the gateway repository the way pool acquisition did.

Before F-06 every function in ``mnemos.db.openai_compat_repo`` opened its raw
``_pool.acquire()`` inside the ``try`` that turned a failure into ``[]`` /
``None`` (or, for the single-model lookup, ``ModelRegistryUnavailable``, which
the route maps to a 503). The ABC bridge opens ``backend.transactional()``
outside the per-call ``try``, so a failure to open the transaction would escape
as a raw exception and become a 500.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

import mnemos.core.lifecycle as lc
from mnemos.db import openai_compat_repo as repo
from mnemos.persistence.base import ModelRegistryUnavailable


class _Boom(RuntimeError):
    pass


class _AuditRaises:
    async def fetch_model_provider(self, tx, model_id):
        raise _Boom("registry read blew up")


class _BackendTransactionFails:
    """A backend whose ``transactional()`` cannot be entered (pool exhausted)."""

    memories = None
    consultations_audit = None

    @asynccontextmanager
    async def transactional(self):
        raise _Boom("cannot open a transaction")
        yield  # pragma: no cover


class _BackendLeafFails:
    memories = None
    consultations_audit = _AuditRaises()

    @asynccontextmanager
    async def transactional(self):
        yield object()


def _install(monkeypatch, backend):
    monkeypatch.setattr(lc, "_pool", None)
    monkeypatch.setattr(lc, "_persistence_backend", backend)


class _User:
    user_id = "u1"
    role = "user"
    namespace = "default"
    group_ids: tuple = ()


def test_memory_context_degrades_to_empty_when_the_transaction_cannot_open(monkeypatch):
    _install(monkeypatch, _BackendTransactionFails())
    assert asyncio.run(repo.fetch_memory_context("q", _User(), limit=3)) == []


def test_model_recommendation_degrades_to_none_when_the_transaction_cannot_open(monkeypatch):
    _install(monkeypatch, _BackendTransactionFails())
    assert asyncio.run(repo.fetch_model_recommendation("reasoning")) is None


def test_provider_lookup_degrades_to_none_when_the_transaction_cannot_open(monkeypatch):
    _install(monkeypatch, _BackendTransactionFails())
    assert asyncio.run(repo.lookup_provider_for_model("m")) is None


def test_available_models_degrades_to_empty_when_the_transaction_cannot_open(monkeypatch):
    _install(monkeypatch, _BackendTransactionFails())
    assert asyncio.run(repo.fetch_available_models()) == []


def test_single_model_lookup_reports_unavailable_when_the_transaction_cannot_open(monkeypatch):
    _install(monkeypatch, _BackendTransactionFails())
    with pytest.raises(ModelRegistryUnavailable):
        asyncio.run(repo.fetch_model_provider("m"))


def test_single_model_lookup_reports_unavailable_when_the_read_fails(monkeypatch):
    _install(monkeypatch, _BackendLeafFails())
    with pytest.raises(ModelRegistryUnavailable):
        asyncio.run(repo.fetch_model_provider("m"))
