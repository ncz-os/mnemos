# Why `tests/test_knossos_phase1.py` errors at collection (2026-11-01)

## The error

```
ERROR tests/test_knossos_phase1.py - AttributeError: 'Server' object has no attribute 'list_tools'
```

It is an **environment** failure, not a code defect: the installed `mcp` package is outside the
range the repo declares.

## Root cause

`mnemos/tools/knossos_mcp.py` registers its tool table with the mcp low-level *convenience
decorators*:

- `mnemos/tools/knossos_mcp.py:75` — `server = Server("knossos")`
- `mnemos/tools/knossos_mcp.py:590` — `@server.list_tools()`
- `mnemos/tools/knossos_mcp.py:602` — `@server.call_tool()`

`mcp` 2.x removed those two decorators. The low-level `Server` in 2.x keeps only the generic
registration API (`add_request_handler()`, `add_notification_handler()`, `get_request_handler()`), so
`server.list_tools` does not exist and the module raises `AttributeError` **while being imported**.
Because the import happens at module scope, pytest reports it as a collection error for the file, not
as a test failure — which is why every full-suite run listed it in the error section.

Reproduced against the real package in a throwaway venv (mcp 2.3.0 only):

```
mcp 2.3.0
REPRODUCED: 'Server' object has no attribute 'list_tools'
```

## Which version range works

Wheel inspection of every release boundary that matters (`python -m pip download --no-deps`, then
looking for `def list_tools` in the wheel):

| mcp version | `Server.list_tools` / `call_tool` | shim imports? |
| --- | --- | --- |
| 0.9.1 | present, `mcp/server/__init__.py` | yes |
| 1.0.0 | present, `mcp/server/__init__.py` | yes |
| 1.1.0 / 1.1.1 | present, `mcp/server/__init__.py` | yes |
| 1.2.0, 1.5.0, 1.7.0, 1.9.0, 1.9.4, 1.10.0, 1.20.0 (sampled) | present, `mcp/server/lowlevel/server.py` | yes |
| 1.30.0 (latest 1.x, installed here) | present, `mcp/server/lowlevel/server.py` | yes |
| 2.0.0 | **absent** | no — `AttributeError` |
| 2.3.0 (latest overall) | **absent** | no — `AttributeError` |

**Works: `mcp>=1.0.0,<2` — exactly what `pyproject.toml:74` and `requirements.txt:61` already pin.
Breaks: `mcp>=2.0.0`.** There is no *too old* boundary inside 1.x: even 0.9.1 still defines the
decorators. The report's earlier guess ("installed mcp versus what the test expects") is right in
direction but the break is at the 2.0 line, not somewhere inside 1.x.

On this workstation (`mcp` 1.30.0, Python 3.14.5) the file is healthy: `pytest
tests/test_knossos_phase1.py -q` → **31 passed**, and `pytest tests/ --collect-only -q` → **5794
tests collected, 0 errors**. So the "known baseline failure" was an environment that had mcp 2.x
installed — out of the declared range.

## What changed (test file + docs only, no production code)

`tests/test_knossos_phase1.py` now gates on the installed version before importing the shim:

- `_mcp_skip_reason()` returns a precise reason when `mcp` is absent or `>= 2.0.0`, and `None`
  otherwise. A version string it cannot parse returns `None` — an unknown version never becomes a
  silent skip.
- The module-level import is wrapped so a genuine `ImportError` also skips with its own message
  instead of erroring the run.
- Any other exception from the import still fails the run: the gate skips on a **version verdict**
  only, so a real regression in the shim cannot be hidden.
- Four tests cover the gate: every supported 1.x is accepted (1.0.0 … 1.30.0), 2.x is rejected with
  the version and `list_tools` named in the reason, an unknown version is not skipped, and a missing
  distribution produces the "not installed" reason.

Observed behaviour:

```
# mcp 1.30.0
35 passed in 0.52s

# same tree with mcp 2.3.0 metadata first on sys.path
SKIPPED [1] tests/test_knossos_phase1.py:75: mcp 2.3.0 dropped Server.list_tools()/call_tool(),
which mnemos.tools.knossos_mcp registers with (needs mcp>=1.0.0,<2)
1 skipped in 0.04s
```

`CONTRIBUTING.md` gained one line in the test section pointing at the requirement and this note.

## Recommendation (not applied — production code is out of scope for this task)

1. Keep the pin `mcp>=1.0.0,<2` in `pyproject.toml` and `requirements.txt`. It is correct.
2. Any image or developer environment that reported this error has mcp 2.x installed; reinstall the
   declared range (`pip install 'mcp>=1.0.0,<2'`) rather than widening the pin.
3. If the shim should ever support mcp 2.x, that is a real port: `@server.list_tools()` and
   `@server.call_tool()` become `add_request_handler()` registrations, and the `TOOLS` table plus the
   dispatcher in `mnemos/tools/knossos_mcp.py` would need to be rebuilt on the new API. Not attempted
   here.
