"""Write-behind cache-aside CooldownStore (GRAEAE mandate §A).

Composes a fast process-local L1 (:class:`InMemoryCooldownStore`) in front of a
durable backend (:class:`SqliteCooldownStore`, or an Oracle one later) so the
hot path never blocks on the DB:

  * COOLDOWNS are read-through: served from L1, and on an L1 miss read from the
    durable store and cached — so a cooldown set by a prior process run (or,
    eventually, a peer) is honored after restart. Writes go to L1 immediately
    and are buffered for a batched flush.
  * COUNTERS are L1-authoritative per process+minute (LiteLLM in-process
    semantics; minute buckets are ephemeral and reset on restart). Increments
    are applied to L1 immediately and buffered as deltas for the flush.

Writes are flushed to the durable store in batches — automatically once
``flush_threshold`` ops are pending, or explicitly via :meth:`flush` (call it on
a periodic timer). This keeps durable I/O off the per-request path while bounding
how much state a crash can lose.
"""

from __future__ import annotations

from threading import Lock

from mnemos.domain.pantheon.cooldown import CooldownStore, InMemoryCooldownStore

DEFAULT_FLUSH_THRESHOLD = 20


class WriteBehindCooldownStore(CooldownStore):
    """L1 + durable backend with read-through cooldowns and batched write-behind."""

    def __init__(
        self,
        durable: CooldownStore,
        *,
        l1: CooldownStore | None = None,
        flush_threshold: int = DEFAULT_FLUSH_THRESHOLD,
    ) -> None:
        self._durable = durable
        self._l1 = l1 if l1 is not None else InMemoryCooldownStore()
        self._lock = Lock()
        self._pending: list[tuple] = []
        self._flush_threshold = max(1, flush_threshold)

    # ── cooldowns: read-through + buffered write ──────────────────────────
    def get_cooled_until(self, tenant: str, deployment: str) -> float | None:
        value = self._l1.get_cooled_until(tenant, deployment)
        if value is not None:
            return value
        value = self._durable.get_cooled_until(tenant, deployment)
        if value is not None:
            self._l1.set_cooldown(tenant, deployment, value)  # populate L1
        return value

    def set_cooldown(self, tenant: str, deployment: str, cooled_until: float) -> None:
        self._l1.set_cooldown(tenant, deployment, cooled_until)
        self._buffer(("cooldown", tenant, deployment, cooled_until))

    # ── counters: L1-authoritative per process+minute, buffered deltas ────
    def incr(self, tenant: str, deployment: str, minute: int, *, success: bool) -> None:
        self._l1.incr(tenant, deployment, minute, success=success)
        self._buffer(("incr", tenant, deployment, minute, success))

    def get_counts(self, tenant: str, deployment: str, minute: int) -> tuple[int, int]:
        return self._l1.get_counts(tenant, deployment, minute)

    # ── write-behind machinery ────────────────────────────────────────────
    def _buffer(self, op: tuple) -> None:
        with self._lock:
            self._pending.append(op)
            over = len(self._pending) >= self._flush_threshold
        if over:
            self.flush()

    def flush(self) -> int:
        """Drain buffered writes to the durable store. Returns ops flushed.

        On a durable-write failure mid-flush, the unflushed remainder is
        requeued (ahead of any newly-buffered ops) before re-raising, so no
        write is silently lost during a DB/pool outage.
        """
        with self._lock:
            ops, self._pending = self._pending, []
        done = 0
        try:
            for op in ops:
                kind = op[0]
                if kind == "cooldown":
                    _, tenant, deployment, cooled_until = op
                    self._durable.set_cooldown(tenant, deployment, cooled_until)
                else:  # "incr"
                    _, tenant, deployment, minute, success = op
                    self._durable.incr(tenant, deployment, minute, success=success)
                done += 1
        except Exception:
            with self._lock:
                self._pending = ops[done:] + self._pending
            raise
        return done

    @property
    def pending(self) -> int:
        with self._lock:
            return len(self._pending)
