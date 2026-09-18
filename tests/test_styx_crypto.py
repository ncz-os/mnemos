"""STYX encryption — real age format, real round trip."""

from __future__ import annotations

import pytest

from mnemos.tools.styx.crypto import (
    AGE_MAGIC,
    encrypt_file,
    has_age_header,
    md5_file,
    sha256_file,
)
from mnemos.tools.styx.errors import StyxConfigError

pyrage = pytest.importorskip("pyrage", reason="STYX encryption needs the styx extra")


@pytest.fixture
def keypair():
    identity = pyrage.x25519.Identity.generate()
    return identity, str(identity.to_public())


def test_encrypt_produces_a_real_age_file_that_decrypts(tmp_path, keypair):
    """The bytes must be readable by any age implementation, not just ours."""
    identity, recipient = keypair
    plain = tmp_path / "bundle.tar.gz"
    payload = b"AKIAIOSFODNN7EXAMPLE not a real key" * 500
    plain.write_bytes(payload)

    encrypted = encrypt_file(plain, tmp_path / "bundle.age", recipient)

    assert encrypted.read_bytes().startswith(AGE_MAGIC)
    assert has_age_header(encrypted)
    # The secret must not be sitting in the ciphertext.
    assert b"AKIAIOSFODNN7EXAMPLE" not in encrypted.read_bytes()
    # And it must come back out intact.
    assert pyrage.decrypt(encrypted.read_bytes(), [identity]) == payload


def test_encrypt_rejects_a_bad_recipient(tmp_path):
    plain = tmp_path / "x"
    plain.write_bytes(b"data")
    with pytest.raises(StyxConfigError, match="not a usable age recipient"):
        encrypt_file(plain, tmp_path / "x.age", "age1definitelynotavalidrecipient")


def test_has_age_header_is_false_for_plaintext(tmp_path):
    plain = tmp_path / "plain.tar.gz"
    plain.write_bytes(b"\x1f\x8b plain gzip, not encrypted")
    assert has_age_header(plain) is False


def test_digests_match_hashlib(tmp_path):
    import hashlib

    path = tmp_path / "blob"
    data = b"styx" * 100_000  # larger than the streaming chunk
    path.write_bytes(data)

    assert sha256_file(path) == hashlib.sha256(data).hexdigest()
    assert md5_file(path) == hashlib.md5(data).hexdigest()  # noqa: S324
