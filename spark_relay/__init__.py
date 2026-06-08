"""Spark<->PYTHIA cloud-object relay (E2EE GCS transport).

See ``docs/SPARK_HIVE_BRIDGE.md`` for the architecture. Two stateless pollers,
one bucket, no on-prem bridge host:

    enqueuer (PYTHIA)  -> seal -> bucket pending/
    spark_poller (Spark) -> claim/execute -> bucket results/
    reconciler (PYTHIA) -> open -> hive done + ARGONAS fan-out
"""

__all__ = ["relay_crypto", "relay_client"]
