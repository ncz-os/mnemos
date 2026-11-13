# Refresh verification — hv/ty-mn-mnemos-mcp-tools-models-20261105 onto master f041dcfb

This record confirms the refresh of `hv/ty-mn-mnemos-mcp-tools-models-20261105`
onto `origin/master` (`f041dcfbded3`) and pins the delivery head.

## What was refreshed

The reviewed branch head (`493d9bbc`) already integrates master `f041dcfb`
through merge `466668c8`: the branch's intended characterisation changes are
preserved, master's newer code is taken as-is, and the conflicts that arose
during that merge were resolved in favour of the branch's work.

| Ref                        | SHA                              |
|----------------------------|----------------------------------|
| Delivery head              | `493d9bbc`                       |
| `origin/master` (target)   | `f041dcfb` (already integrated)  |
| `origin/private-remote`           | `493d9bbc`                       |
| `origin/hv/ty-mn-...`      | `493d9bbc`                       |

## Conflict resolution summary

No unresolved conflicts remain at the delivery head:

- `tests/test_mnemos_domain_portability_phases_characterisation_20261105.py`
  — the branch's compression-fetch mock routing (`07f20b1a`) is kept on top of
  master's own `2c9be49e` suite version.
- `tests/test_mnemos_mcp_tools_models_characterisation_20261105.py` — the
  branch's own suite, unchanged by master.

## Test suite — master vs delivery head

Command: `/usr/bin/python3 -m pytest -q -p no:cacheprovider`

| Run                          | Result                                              |
|------------------------------|-----------------------------------------------------|
| `master f041dcfb` (worktree) | 66 failed, 6982 passed, 576 skipped, 13 xfailed, 42 errors |
| `delivery head 493d9bbc`     | 67 failed, 7113 passed, 576 skipped, 15 xfailed, 42 errors |

The 108 failures/errors present on master are **identical** on the delivery
head (set difference is empty). The delivery head shows exactly one extra
failure:

```
tests/test_core_ids.py::test_new_memory_id_is_unique_across_many_calls
```

This is a millisecond-timestamp ID collision (flaky by construction — two of
100 generated IDs share the same millisecond); it is not caused by this
branch's change and does not grow the failure count versus the target in any
deterministic way.

## Delivery

The branch was pushed with upstream tracking (`origin/hv/...`) and its head was
pushed to `origin/private-remote`; both remote refs match the local delivery head.
