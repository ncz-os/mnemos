"""Release gate 2026-11-12: the MN-07 detectors must leave ordinary fleet memories alone.

Measured on a copy of the production corpus (41,836 live memories): bare 40-hex git SHAs were read as AWS
secret keys and vaulted, and a memory id followed by a newline was joined into the next sentence and masked.
Real credentials must stay caught.
"""

from __future__ import annotations

import pytest

from mnemos.core.secret_detection import SecretClass, classify, redact_content

SHA = "e4cacdf341f83a6e2f1e135ae21262e7ec9d293a"
SHA2 = "0773fd878615f0a1c2b3d4e5f60718293a4b5c6d"


@pytest.mark.parametrize(
    "text",
    [
        f"commits=['{SHA}', '{SHA2}']",
        f"fleet-ops main {SHA} (muse approve)",
        f"merged at {SHA} after review",
        f"PUSH   f61840dc..d57db3bb {SHA}",
        f"ARGONAS now {SHA2}; GitLab behind by 3",
        f"head {SHA.upper()} on the deploy line",
        "Deployed branch usr/local/share/mnemos/config/files",
        "Hive job 01a1d103-ccfd-7a5c-b0e1-3f2a9c4d5e6f\nCategory infrastructure done",
        "Hive job 01a1d103-ccfd-7a5c-b0e1-3f2a9c4d5e6f\n  Category infrastructure done and verified",
        "Review of job 01a1d0cf-c8b8-792d-8dce-ed1edb38898b\nResult approved by the second reviewer",
    ],
)
def test_ordinary_fleet_text_stays_clean(text):
    finding = classify(text)
    assert finding.cls == SecretClass.CLEAN, (finding.cls, finding.reasons)
    assert redact_content(text) == text


@pytest.mark.parametrize(
    "text",
    [
        "the AWS secret access key is wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "AKIAIOSFODNN7EXAMPLE wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "secret: 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",
    ],
)
def test_real_aws_shapes_are_still_caught(text):
    finding = classify(text)
    assert finding.cls != SecretClass.CLEAN


def test_a_wrapped_token_is_still_reassembled():
    text = "token: abcdefghijklmn\nopqrstuvwxyz0123456789"
    assert classify(text).cls != SecretClass.CLEAN
