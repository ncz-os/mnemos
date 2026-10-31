"""Unit tests for ``scripts/audit_secret_metadata_markers.py`` (M1 audit tool).

The script itself is read-only and is NOT run against any database here:
these tests exercise the pure detection helpers only.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "audit_secret_metadata_markers.py"


def _load():
    spec = importlib.util.spec_from_file_location("audit_secret_metadata_markers", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_flags_degenerate_redact_fields():
    mod = _load()
    problems = mod.find_suspicious_keys({"secret_redact_fields": {"content": [[0, 0]]}})
    assert "secret_redact_fields.content:masks_nothing" in problems


def test_flags_degenerate_legacy_spans_and_empty_list():
    mod = _load()
    assert mod.find_suspicious_keys({"secret_redact_spans": []}) == ["secret_redact_spans:masks_nothing"]
    assert mod.find_suspicious_keys({"secret_redact_spans": [[9, 3]]}) == ["secret_redact_spans:masks_nothing"]


def test_real_span_and_server_stamps_are_not_flagged():
    mod = _load()
    clean = {
        "secret_redact_fields": {"content": [[41, 105]]},
        "secret_redact_spans": [[41, 105]],
        "secret_classified_at": "ingest",
        "secret_reasons": ["content:hex64"],
        "team": "infra",
    }
    assert mod.find_suspicious_keys(clean) == []


def test_flags_forged_classification_stamp():
    mod = _load()
    problems = mod.find_suspicious_keys({"secret_classified_at": "forged"})
    assert any(p.startswith("secret_classified_at:unknown_stamp") for p in problems)


def test_scan_reports_ids_without_content(capsys):
    mod = _load()
    rows = [
        {"id": "clean-1", "owner_id": "a", "namespace": "n", "metadata": json.dumps({"note": "ok"})},
        {
            "id": "planted-1",
            "owner_id": "a",
            "namespace": "n",
            "metadata": json.dumps({"secret_redact_fields": {"content": [[0, 0]]}}),
        },
    ]
    scanned, flagged = mod.scan(rows)
    assert (scanned, flagged) == (2, 1)
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1
    report = json.loads(out[0])
    assert report["id"] == "planted-1"
    assert "secret_redact_fields.content:masks_nothing" in report["problems"]
