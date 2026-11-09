# MN-17 / CF-35 refresh verification — hv/bl-mn-17-20261107 onto master (450dcf52)

Date: 2026-11-08 (UTC-05)

## Task

Refresh the reviewed-and-approved branch `hv/bl-mn-17-20261107` onto
`master` (now `450dcf520777`), which is no longer a fast-forward base for the
branch. Merge `origin/master` into the branch, resolve every conflict keeping
the branch's intended change and the target's newer code, run the repository
test suite before and after (failures must not grow versus the target), and
push the same head branch to ARGONAS. No new features, no unrelated files.

## What the merge produced

The branch head `5721013d` ("Merge origin/master (450dcf52) into
hv/bl-mn-17-20261107 (refresh)") is a two-parent merge of the previously
refreshed branch head `868d3094` (already carrying master `e4cacdf3`) and
`origin/master` `450dcf52`.

`git merge-base --is-ancestor 450dcf52 HEAD` is true, so master is a
ancestor of the delivered head and the branch IS a fast-forward-able refresh
of `450dcf52`.

**Zero conflicts.** Master's only new content since `e4cacdf3` is:

- `50730e50` — webhook/document-import secret + namespace redaction
  (`mnemos/api/routes/document_import.py`, `mnemos/domain/document_repo.py`,
  `tests/test_document_import_webhook_redacted.py`)
- `6a7f730e` / `e4cacdf3` — release 7.1.0 / 7.1.1 (version pins, changelog,
  Oracle reserved-word bind renames in `mnemos/persistence/oracle.py`)
- `9c0ee9cd` / `b207dc5b` / `450dcf52` — ADR-D-MN5 decision record
  (`docs/adr/ADR-D-MN5-rows-fenced-before-the-replica-fence-exi.md`)

None of those paths intersect this branch's change, so the merge applied
cleanly and master's newer code is present verbatim. Verified: the three
`50730e50` source/test files are byte-identical to `origin/master`
(`git diff --quiet origin/master HEAD -- <path>` on each is empty), and the
Oracle rename plus the 7.1.1 version pin (`pyproject.toml version =
"7.1.1"`) are in the delivered tree.

The branch's intended change — MN-17/CF-35, the injective NATS subject-token
encoder — is preserved untouched on top of master:

- `mnemos/persistence/nats_events.py` — `safe_subject_segment` keeps
  `[A-Za-z0-9-]` verbatim, renders `.` -> `_`, percent-encodes everything else
  per UTF-8 byte (so `a_b` -> `a%5Fb` never collides with `a.b` -> `a_b`)
- `mnemos/nats/webhook_events.py`, `mnemos/mcp/http.py`,
  `mnemos/domain/memory_dedup.py`, `mnemos/api/routes/webhooks.py` — every
  namespace sink delegates to that one shared encoder instead of a local
  `.replace(".", "_")` copy
- `tests/test_nats_subject_namespace_collision.py` (new, 243 lines) plus the
  updated assertion in `tests/test_nats_publishers_wired.py`
- `docs/NATS_SUBSTRATE.md` — documents the shared encoder and the injectivity
  rule

No conflict required a design decision.

## Test suite (target vs branch)

Full suite `python3 -m pytest tests/ -q --tb=no -p no:randomly` on this host
(Python 3.14.10, pytest 9.1.1), run against a detached worktree of the target
`450dcf52` and against the delivered branch head:

- Target `450dcf52`: **69 failed, 6246 passed, 576 skipped, 9 xfailed,
  3 xpassed, 42 errors**
- Branch head `5721013d`: **69 failed, 6281 passed, 576 skipped, 9 xfailed,
  3 xpassed, 42 errors**

Failures did **not** grow: 69 failed / 42 errors on both sides, and a sorted
diff of the per-test `FAILED` + `ERROR` lines is **empty** — the failure set
is byte-identical. The passing count grows by 35 (6246 -> 6281), which is
this branch's own NATS subject-escaping coverage.

MN-17-owned suites in isolation
(`tests/test_nats_subject_namespace_collision.py` +
`tests/test_nats_publishers_wired.py`): **43 passed**.

Ratchet guard `scripts/check_abc_first_guard.py` (`lint:abc-first`): **passed**,
no increase in raw `_lc._pool` references outside `mnemos/persistence/`.

## Conclusion

Failures did not grow versus the target `450dcf52`, master's newer code
(webhook redaction, release 7.1.1 / Oracle bind renames, ADR-D-MN5) is in the
tree verbatim, and the intended MN-17/CF-35 change is preserved. The same
head branch `hv/bl-mn-17-20261107` was pushed to ARGONAS (`origin`) with
upstream tracking; the remote SHA is verified equal to local HEAD.
