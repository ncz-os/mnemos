from __future__ import annotations

from pathlib import Path
import re
import tomllib


ROOT = Path(__file__).resolve().parents[1]


def test_mcp_dependency_excludes_incompatible_major_version() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = project["project"]["dependencies"]

    assert "mcp>=1.0.0,<2" in dependencies
    assert re.search(r"^mcp>=1\.0\.0,<2$", (ROOT / "requirements.txt").read_text(encoding="utf-8"), re.MULTILINE)


def test_runtime_recovery_commands_name_the_installable_distribution() -> None:
    production_text = "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "mnemos").rglob("*.py"))

    assert "mnemos-os[" not in production_text
