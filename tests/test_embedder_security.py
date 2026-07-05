from __future__ import annotations


def test_embedder_remote_code_trust_defaults_off(monkeypatch):
    from mnemos.runtime.embedder import InProcessEmbedder

    monkeypatch.delenv("MNEMOS_EMBED_TRUST_REMOTE_CODE", raising=False)

    embedder = InProcessEmbedder(backend="openvino")
    ov_backend = embedder._make_backend("openvino")
    cix_backend = embedder._make_backend("cix-npu")

    assert embedder.trust_remote_code is False
    assert ov_backend.trust_remote_code is False
    assert cix_backend.trust_remote_code is False


def test_embedder_remote_code_trust_requires_explicit_opt_in(monkeypatch):
    from mnemos.runtime.embedder import InProcessEmbedder

    monkeypatch.setenv("MNEMOS_EMBED_TRUST_REMOTE_CODE", "yes")

    embedder = InProcessEmbedder(backend="openvino")
    ov_backend = embedder._make_backend("openvino")
    cix_backend = embedder._make_backend("cix-npu")

    assert embedder.trust_remote_code is True
    assert ov_backend.trust_remote_code is True
    assert cix_backend.trust_remote_code is True
