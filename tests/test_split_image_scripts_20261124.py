"""Unit tests for the three Dockerfile.split helper scripts.

Covers scripts/merge_split_requirements.py, scripts/audit_split_namespace.py
and scripts/resolve_split_wheels.py by loading each file with
importlib.util.spec_from_file_location and driving main() with patched
sys.argv. Every output path lives under tmp_path; script defaults that
write to /tmp are never used.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


def load_script(name: str):
    path = SCRIPTS_DIR / name
    mod_name = f"split_{path.stem}"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before executing so the module is discoverable by name; scripts
    # that use dataclasses, typing.get_type_hints, pickle or relative imports
    # fail or misbehave without a sys.modules entry.
    sys.modules[mod_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(mod_name, None)
        raise
    return module


@pytest.fixture()
def merge_mod():
    return load_script("merge_split_requirements.py")


@pytest.fixture()
def audit_mod():
    return load_script("audit_split_namespace.py")


@pytest.fixture()
def resolve_mod():
    return load_script("resolve_split_wheels.py")


def write_core(tmp_path: Path, optional: dict, requirements: str) -> Path:
    core = tmp_path / "core"
    core.mkdir()
    lines = ["[project]", 'name = "mnemos-core"', "[project.optional-dependencies]"]
    for extra, deps in optional.items():
        rendered = ", ".join(f'"{d}"' for d in deps)
        lines.append(f"{extra} = [{rendered}]")
    (core / "pyproject.toml").write_text("\n".join(lines) + "\n")
    (core / "requirements.txt").write_text(requirements)
    return core


def write_addon(tmp_path: Path, name: str, deps: list[str]) -> Path:
    addon = tmp_path / name
    addon.mkdir()
    rendered = ", ".join(f'"{d}"' for d in deps)
    (addon / "pyproject.toml").write_text(
        "[project]\n"
        f'name = "mnemos-{name}"\n'
        f"dependencies = [{rendered}]\n"
    )
    return addon


def run_merge(merge_mod, monkeypatch, capsys, core: Path, addons: list[Path], out: Path):
    # --addons takes nargs="*" so pass all add-on paths at once.
    argv = ["merge_split_requirements.py", "--core", str(core),
            "--addons", *(str(a) for a in addons),
            "--output", str(out)]
    monkeypatch.setattr(sys, "argv", argv)
    rc = merge_mod.main()
    captured = capsys.readouterr()
    return rc, captured


# --- merge_split_requirements ---

def test_merge_drops_first_party_plain(merge_mod, tmp_path, monkeypatch, capsys):
    core = write_core(tmp_path, {}, "requests>=2\n")
    addon = write_addon(tmp_path, "pantheon",
                         ["mnemos-core", "mnemos-pantheon", "mnemos-knemon",
                          "mnemos-graeae", "requests>=2"])
    out = tmp_path / "req.txt"
    rc, _ = run_merge(merge_mod, monkeypatch, capsys, core, [addon], out)
    assert rc == 0
    lines = out.read_text().splitlines()
    assert "mnemos-core" not in lines
    assert "mnemos-pantheon" not in lines
    assert lines.count("requests>=2") == 1


def test_merge_first_party_normalisation(merge_mod, tmp_path, monkeypatch, capsys):
    assert merge_mod.parse_req("mnemos_pantheon>=1")[0] == "mnemos-pantheon"
    assert merge_mod.parse_req("MNEMOS-KNEMON")[0] == "mnemos-knemon"
    assert merge_mod.parse_req("mnemos_graeae[x]")[0] == "mnemos-graeae"
    core = write_core(tmp_path, {}, "plain>=1\n")
    addon = write_addon(tmp_path, "knemon",
                         ["mnemos_pantheon", "MNEMOS-KNEMON", "MNEMOS_GRAEAE", "plain>=1"])
    out = tmp_path / "req.txt"
    rc, _ = run_merge(merge_mod, monkeypatch, capsys, core, [addon], out)
    assert rc == 0
    lines = out.read_text().splitlines()
    # Assert each first-party package is absent as a standalone entry rather
    # than only as a substring. oracledb>=4.0.1 is always appended by the
    # writer and is legitimate, so it is simply not a first-party name here;
    # no line may carry a first-party name in any form.
    first_party = ("mnemos-core", "mnemos-pantheon", "mnemos-knemon", "mnemos-graeae")
    for pkg in first_party:
        assert not any(merge_mod.parse_req(ln)[0] == pkg for ln in lines), (
            f"{pkg!r} leaked into merged requirements: {lines!r}"
        )


def test_merge_core_extra_expands_recursively(merge_mod, tmp_path, monkeypatch, capsys):
    optional = {
        "server": ["fastapi>=0.1", "mnemos-core[extra2]"],
        "extra2": ["httpx"],
    }
    core = write_core(tmp_path, optional, "a>=1\n")
    addon = write_addon(tmp_path, "pantheon", ["mnemos-core[server]"])
    out = tmp_path / "req.txt"
    rc, _ = run_merge(merge_mod, monkeypatch, capsys, core, [addon], out)
    assert rc == 0
    lines = out.read_text().splitlines()
    assert "fastapi>=0.1" in lines
    assert "httpx" in lines
    assert not any(ln.startswith("mnemos-core") for ln in lines)


def test_merge_core_extra_self_reference_no_loop(merge_mod, tmp_path, monkeypatch, capsys):
    optional = {"loop": ["mnemos-core[loop]", "solo>=9"]}
    core = write_core(tmp_path, optional, "a>=1\n")
    addon = write_addon(tmp_path, "graeae", ["mnemos-core[loop]"])
    out = tmp_path / "req.txt"
    rc, _ = run_merge(merge_mod, monkeypatch, capsys, core, [addon], out)
    assert rc == 0
    lines = out.read_text().splitlines()
    assert lines.count("solo>=9") == 1


def test_merge_pip_option_dropped_with_lineno(merge_mod, tmp_path, monkeypatch, capsys):
    req_txt = "# a comment\n\n-r other.txt\nplain>=1\n"
    core = write_core(tmp_path, {}, req_txt)
    addon = write_addon(tmp_path, "pantheon", [])
    out = tmp_path / "req.txt"
    rc, captured = run_merge(merge_mod, monkeypatch, capsys, core, [addon], out)
    assert rc == 0
    lines = out.read_text().splitlines()
    assert not any(ln.startswith("-r") for ln in lines)
    assert not any(ln.startswith("#") for ln in lines)
    assert "plain>=1" in lines
    assert ":3:" in captured.err
    assert "-r other.txt" in captured.err


def test_merge_duplicates_preserve_first_order(merge_mod, tmp_path, monkeypatch, capsys):
    core = write_core(tmp_path, {}, "b>=1\na>=1\nb>=1\n")
    addon = write_addon(tmp_path, "pantheon", ["c>=1", "a>=1"])
    out = tmp_path / "req.txt"
    rc, _ = run_merge(merge_mod, monkeypatch, capsys, core, [addon], out)
    assert rc == 0
    lines = out.read_text().splitlines()
    body = [ln for ln in lines if ln != "oracledb>=4.0.1"]
    assert body == ["b>=1", "a>=1", "c>=1"]


def test_merge_output_ends_with_oracledb(merge_mod, tmp_path, monkeypatch, capsys):
    core = write_core(tmp_path, {}, "z>=1\n")
    addon = write_addon(tmp_path, "knemon", [])
    out = tmp_path / "req.txt"
    rc, _ = run_merge(merge_mod, monkeypatch, capsys, core, [addon], out)
    assert rc == 0
    text = out.read_text()
    assert text.endswith("oracledb>=4.0.1\n")
    assert text.splitlines()[-1] == "oracledb>=4.0.1"


# --- audit_split_namespace ---

def run_audit(audit_mod, monkeypatch, capsys, roots: list[str]):
    monkeypatch.setattr(sys, "argv", ["audit_split_namespace.py", *roots])
    rc = audit_mod.main()
    captured = capsys.readouterr()
    return rc, captured


def make_root(tmp_path: Path, name: str, files: list[str]) -> Path:
    root = tmp_path / name
    for rel in files:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# pkg\n")
    return root


def test_audit_duplicate_namespace_file(audit_mod, tmp_path, monkeypatch, capsys):
    r1 = make_root(tmp_path, "core", ["mnemos/x.py"])
    r2 = make_root(tmp_path, "pantheon", ["mnemos/x.py"])
    rc, captured = run_audit(audit_mod, monkeypatch, capsys, [str(r1), str(r2)])
    assert rc == 1
    assert "duplicate mnemos namespace file" in captured.err


def test_audit_forbidden_marker(audit_mod, tmp_path, monkeypatch, capsys):
    root = make_root(tmp_path, "core", ["mnemos/__init__.py", "mnemos/ok.py"])
    rc, captured = run_audit(audit_mod, monkeypatch, capsys, [str(root)])
    assert rc == 1
    assert "forbidden namespace marker" in captured.err


def test_audit_forbidden_marker_nested(audit_mod, tmp_path, monkeypatch, capsys):
    root = make_root(tmp_path, "core", ["mnemos/tools/__init__.py"])
    rc, captured = run_audit(audit_mod, monkeypatch, capsys, [str(root)])
    assert rc == 1
    assert "forbidden namespace marker" in captured.err


def test_audit_clean_tree_returns_zero(audit_mod, tmp_path, monkeypatch, capsys):
    r1 = make_root(tmp_path, "core", ["mnemos/a.py"])
    r2 = make_root(tmp_path, "pantheon", ["mnemos/b.py"])
    rc, _ = run_audit(audit_mod, monkeypatch, capsys, [str(r1), str(r2)])
    assert rc == 0


def test_audit_missing_root_returns_zero(audit_mod, tmp_path, monkeypatch, capsys):
    missing = tmp_path / "does-not-exist"
    rc, _ = run_audit(audit_mod, monkeypatch, capsys, [str(missing)])
    assert rc == 0


# --- resolve_split_wheels ---

def run_resolve(resolve_mod, monkeypatch, capsys, wheelhouse: Path, out: Path):
    monkeypatch.setattr(sys, "argv", ["resolve_split_wheels.py",
                                      "--wheelhouse", str(wheelhouse),
                                      "--output", str(out)])
    rc = resolve_mod.main()
    captured = capsys.readouterr()
    return rc, captured


def make_wheel(wheelhouse: Path, stem: str) -> Path:
    wheelhouse.mkdir(parents=True, exist_ok=True)
    wheel = wheelhouse / f"{stem}-1.0-py3-none-any.whl"
    wheel.write_bytes(b"fake-wheel")
    return wheel


def test_resolve_happy_path_order(resolve_mod, tmp_path, monkeypatch, capsys):
    house = tmp_path / "house"
    for stem in ("mnemos_core", "mnemos_pantheon", "mnemos_knemon", "mnemos_graeae"):
        make_wheel(house, stem)
    out = tmp_path / "wheels.txt"
    rc, _ = run_resolve(resolve_mod, monkeypatch, capsys, house, out)
    assert rc == 0
    lines = out.read_text().splitlines()
    assert len(lines) == 4
    assert [Path(p).name.split("-")[0] for p in lines] == [
        "mnemos_core", "mnemos_pantheon", "mnemos_knemon", "mnemos_graeae"]


def test_resolve_zero_wheels_fails(resolve_mod, tmp_path, monkeypatch, capsys):
    house = tmp_path / "house"
    house.mkdir()
    out = tmp_path / "wheels.txt"
    rc, captured = run_resolve(resolve_mod, monkeypatch, capsys, house, out)
    assert rc == 1
    assert "mnemos_core" in captured.err
    assert not out.exists()


def test_resolve_two_wheels_fails_and_lists(resolve_mod, tmp_path, monkeypatch, capsys):
    house = tmp_path / "house"
    house.mkdir(parents=True, exist_ok=True)
    (house / "mnemos_core-1.0-py3-none-any.whl").write_bytes(b"x")
    (house / "mnemos_core-2.0-py3-none-any.whl").write_bytes(b"x")
    for stem in ("mnemos_pantheon", "mnemos_knemon", "mnemos_graeae"):
        make_wheel(house, stem)
    out = tmp_path / "wheels.txt"
    rc, captured = run_resolve(resolve_mod, monkeypatch, capsys, house, out)
    assert rc == 1
    assert "mnemos_core" in captured.err
    assert "mnemos_core-1.0" in captured.err
    assert "mnemos_core-2.0" in captured.err


def test_resolve_output_not_written_on_failure(resolve_mod, tmp_path, monkeypatch, capsys):
    house = tmp_path / "house"
    house.mkdir()
    out = tmp_path / "wheels.txt"
    # A pre-existing output must survive the failure path byte-for-byte: the
    # script returns before it ever opens --output.
    out.write_text("stale\n")
    rc, _ = run_resolve(resolve_mod, monkeypatch, capsys, house, out)
    assert rc == 1
    assert out.exists()
    assert out.read_text() == "stale\n"
