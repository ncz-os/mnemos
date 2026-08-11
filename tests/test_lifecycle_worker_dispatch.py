from __future__ import annotations

from mnemos.api import lifecycle_hooks
from mnemos.core import lifecycle
from mnemos.workers import deletion_request_worker, persephone_archival_worker


def test_postgres_lifecycle_workers_receive_pool_not_backend(monkeypatch):
    pool = object()
    backend = object()
    deletion_calls = []
    archival_calls = []

    monkeypatch.setattr(lifecycle_hooks, "service_enabled", lambda *_args: True)
    monkeypatch.setattr(lifecycle, "_persistence_backend", backend)
    monkeypatch.setattr(
        deletion_request_worker,
        "deletion_request_worker_loop",
        lambda handle, **kwargs: deletion_calls.append((handle, kwargs)) or object(),
    )
    monkeypatch.setattr(
        persephone_archival_worker,
        "persephone_archival_worker_loop",
        lambda handle, **kwargs: archival_calls.append((handle, kwargs)) or object(),
    )

    lifecycle_hooks._deletion_request_worker(pool)
    lifecycle_hooks._persephone_archival_worker(pool)

    assert deletion_calls[0][0] is pool
    assert archival_calls[0][0] is pool

    deletion_calls[0][1]["on_error"](RuntimeError("deletion failed"))
    assert lifecycle._worker_status["deletion_request_worker"] == "error"
    assert lifecycle._worker_status["deletion_request_worker_last_error"] == "deletion failed"

    archival_calls[0][1]["on_success"]()
    assert lifecycle._worker_status["persephone_archival_worker"] == "healthy"
    assert lifecycle._worker_status["persephone_archival_worker_last_success"] > 0
