import json
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import sbom_gate  # noqa: E402


def _report(*items):
    return {
        "matches": [
            {
                "artifact": {"name": name, "version": version},
                "vulnerability": {"id": vid, "severity": sev, "fix": {"versions": fixed}},
            }
            for name, version, vid, sev, fixed in items
        ]
    }


def _entry(**over):
    entry = {
        "package": "pkg",
        "version": "v1.*",
        "vuln_id": "CVE-1",
        "reason": "no upstream fix",
        "review_by": "2026-12-11",
    }
    entry.update(over)
    return entry


TODAY = date(2026, 11, 27)


def test_unaccepted_fixable_high_blocks():
    blocked, expired = sbom_gate.evaluate(_report(("pkg", "v1.2", "CVE-1", "High", ["1.3"])), [], TODAY)
    assert [f["vuln_id"] for f in blocked] == ["CVE-1"] and not expired


def test_accepted_entry_with_version_glob_passes():
    blocked, _ = sbom_gate.evaluate(_report(("pkg", "v1.2", "CVE-1", "Critical", ["1.3"])), [_entry()], TODAY)
    assert blocked == []


def test_unfixable_and_low_findings_do_not_block():
    report = _report(("pkg", "v1.2", "CVE-1", "High", []), ("pkg", "v1.2", "CVE-2", "Medium", ["1.3"]))
    assert sbom_gate.evaluate(report, [], TODAY)[0] == []


def test_expired_entry_fails_and_no_longer_covers():
    report = _report(("pkg", "v1.2", "CVE-1", "High", ["1.3"]))
    blocked, expired = sbom_gate.evaluate(report, [_entry(review_by="2026-11-01")], TODAY)
    assert expired and len(blocked) == 1


def test_wrong_version_or_id_is_not_covered():
    report = _report(("pkg", "v2.0", "CVE-1", "High", ["2.1"]), ("pkg", "v1.2", "CVE-9", "High", ["1.3"]))
    assert len(sbom_gate.evaluate(report, [_entry()], TODAY)[0]) == 2


def test_entry_without_an_id_covers_every_id_for_that_package():
    report = _report(("pkg", "v1.2", "CVE-1", "High", ["1.3"]), ("pkg", "v1.2", "CVE-2", "Critical", ["1.4"]))
    entry = _entry()
    del entry["vuln_id"]
    assert sbom_gate.evaluate(report, [entry], TODAY)[0] == []


def test_incomplete_entry_is_rejected(tmp_path):
    path = tmp_path / "a.json"
    path.write_text(json.dumps({"accepted": [_entry(reason="")]}))
    try:
        sbom_gate.load_accepted(path)
    except ValueError as exc:
        assert "reason" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_shipped_accepted_file_is_valid():
    entries = sbom_gate.load_accepted(ROOT / ".github" / "sbom-accepted-risk.json")
    assert entries and all(e["review_by"] for e in entries)


def test_cli_exit_codes(tmp_path):
    grype = tmp_path / "g.json"
    grype.write_text(json.dumps(_report(("pkg", "v1.2", "CVE-1", "High", ["1.3"]))))
    accepted = tmp_path / "a.json"
    accepted.write_text(json.dumps({"accepted": []}))
    cmd = [sys.executable, str(ROOT / "scripts" / "sbom_gate.py"), "--grype", str(grype), "--accepted", str(accepted)]
    assert subprocess.run(cmd, capture_output=True).returncode == 1
    accepted.write_text(json.dumps({"accepted": [_entry(review_by="2999-01-01")]}))
    assert subprocess.run(cmd, capture_output=True).returncode == 0
