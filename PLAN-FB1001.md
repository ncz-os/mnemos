# PLAN-FB1001 — mnemos: oracle branch repository contract (tests only)

Brief: FREEBUFF-LANE-BRIEF-67.md `### FB1001.` (base `master`).

## Scope
- New file: `tests/test_oracle_branch_repo_20261107.py`.
- Production untouched. Symbols: `mnemos.persistence.oracle.OracleBranchRepository`
  and `mnemos.persistence.base.BranchRepository`.

## Facts established before writing tests
- Base declares four abstract coroutines: `create_memory_branch`,
  `delete_memory_branches_for_memories`, `fetch_memory_branch_heads`,
  `upsert_memory_branch_head`.
- `OracleBranchRepository` defines all four concretely, has no `__init__`, and the module
  contains no `NotImplementedError` outside the unused `_stub_method` factory — so the
  class docstring ("supports upsert head, stubs others") is stale.
- Two methods short-circuit before touching a connection when `memory_ids` is empty, which
  lets the empty path be exercised with `tx=None`.

## Cases
1. `__bases__` is exactly `(BranchRepository,)`.
2. The base declares exactly the four documented abstract methods.
3. Concrete class, argument-free construction.
4. Construction imports no driver and leaves `__dict__` empty.
5. All four defined on the class, concrete, coroutines.
6. None is a stub: no `NotImplementedError` in any implementation; factory still unused.
7. Parameter parity (name, kind, default) with the base for all four.
8. Unknown attributes raise `AttributeError`; no `__getattr__`/`__getattribute__`.
9. Instances share no state.
10. Empty-path behaviour: `fetch_memory_branch_heads(None, [])` returns `[]` and
    `delete_memory_branches_for_memories(None, [])` returns `None` without a transaction.
11. The stale docstring claim is pinned as a mismatch (docstring says stubbed, code does
    not raise).

## Risks / divergences
- `DIVERGENCE:` docstring claims "stubs others" while all four methods are implemented.
- `DIVERGENCE:` `fetch_memory_branch_heads` returns `[]` both for an empty `memory_ids`
  and for an empty `authorized_version_uuids`, indistinguishable from "no branches".

## Verification
`pytest tests/test_oracle_branch_repo_20261107.py -q` plus `ruff check`.
