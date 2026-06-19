# Spark relay — systemd units

Three units. Two run on **PYTHIA** (enqueuer + reconciler), one on the **Spark**
(poller). All read secrets from an `EnvironmentFile` so nothing sensitive lives in
the unit or the repo.

## EnvironmentFile

Create `~/.config/spark-relay/relay.env` (mode 0600) on each host:

```ini
# both sides
SPARK_HIVE_RELAY_E2EE_KEY=<base64-32-bytes>     # identical on PYTHIA + Spark
SPARK_HIVE_RELAY_GCS_SA=/home/<user>/.config/spark-relay/relay-sa.json
SPARK_HIVE_RELAY_BUCKET=spark-hive-relay-bkt
# PYTHIA only
MNEMOS_TOKEN=<mnemos bearer>
HIVE_BASE=http://192.168.207.67:5005
MNEMOS_BASE=http://192.168.207.67:5002
# Spark only
NGC_API_KEY=<host-locked NGC key>
```

Copy the `relay-rw` service-account JSON to the path above (mode 0600). Values are
canonical in `~/.api_keys_master.json` → `spark_hive_relay` on STUDIO.

## Install

```bash
sudo cp spark-relay-enqueuer.service spark-relay-reconciler.service /etc/systemd/system/  # PYTHIA
sudo cp spark-relay-poller.service /etc/systemd/system/                                   # Spark
sudo systemctl daemon-reload
sudo systemctl enable --now spark-relay-enqueuer spark-relay-reconciler                   # PYTHIA
sudo systemctl enable --now spark-relay-poller                                            # Spark
```

Adjust `User=`/`WorkingDirectory=`/venv path per host (units assume `~/mnemos`
checkout with a `.venv`). The Spark unit defaults to user `nvidia`.
