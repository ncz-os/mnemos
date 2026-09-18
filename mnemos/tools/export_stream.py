"""Disk-backed MPF/JSONL export without retaining the corpus in RAM."""

import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

from mnemos.core.config import export_page_max_bytes_env


class ExportValidationError(ValueError):
    """The remote export cannot be published as a complete artifact."""


SIDECARS = (
    "kg_triples",
    "relations",
    "memory_versions",
    "compression_manifest",
    "compression_candidates",
    "embeddings",
    "attestations",
    "deletion_log",
)


def iter_pages(endpoint, api_key, params):
    """Negotiate snapshot streaming, retaining older-server page compatibility."""
    params = {k: v for k, v in params.items() if v is not None}
    headers = {"Accept": "application/x-ndjson"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    def request(query):
        url = endpoint.rstrip("/") + "/v1/export?" + urllib.parse.urlencode(query)
        return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=120)

    with request({**params, "stream": "true"}) as response:
        if "application/x-ndjson" in getattr(response, "headers", {}).get("Content-Type", ""):
            cap = export_page_max_bytes_env()
            while True:
                line = response.readline(cap + 1)
                if not line:
                    raise ExportValidationError("export stream ended before completion marker")
                if len(line) > cap:
                    raise ExportValidationError("export page exceeds configured byte limit")
                page = json.loads(line)
                if not isinstance(page, dict):
                    raise ExportValidationError("export page must be an object")
                yield page
                if page.get("export_complete") is True:
                    return
        else:
            first = json.load(response)
    print(
        "WARNING: older server has no snapshot streaming; pause mutations for a consistent export.",
        file=sys.stderr,
    )
    offset = int(params.get("offset", 0))
    limit = int(params.get("limit", 1000))
    page = first
    count = 0
    last_fingerprint = None
    for _ in range(10000):
        if not isinstance(page, dict) or not isinstance(page.get("records"), list):
            raise ExportValidationError("legacy export response has no records array")
        records = page["records"]
        fingerprint = hashlib.sha256(json.dumps(records, sort_keys=True).encode()).digest()
        if records and fingerprint == last_fingerprint:
            raise ExportValidationError("legacy export pagination did not advance")
        last_fingerprint = fingerprint
        yield page
        count += len(records)
        if len(records) < limit:
            break
        offset += len(records)
        with request({**params, "offset": offset}) as response:
            page = json.load(response)
    else:
        raise ExportValidationError("legacy export exceeded 10000 pages")
    # Tombstones have their own cursor, independent of memory offsets.
    cursor = first.get("deletion_log_next_cursor")
    seen = set()
    while cursor:
        if cursor in seen:
            raise ExportValidationError("legacy deletion cursor did not advance")
        seen.add(cursor)
        deletion_params = {
            k: v
            for k, v in params.items()
            if k not in {"owner_id", "namespace", "deletion_log_from", "deletion_log_to"}
        }
        with request({**deletion_params, "offset": 0, "limit": 1, "deletion_log_cursor": cursor}) as response:
            deletion = json.load(response)
        yield {"records": [], "deletion_log": deletion.get("deletion_log", [])}
        cursor = deletion.get("deletion_log_next_cursor")
    yield {"export_complete": True, "record_count": count}


def _identity(surface, entry):
    if surface == "records":
        if not isinstance(entry.get("id"), str) or not entry["id"]:
            raise ExportValidationError("exported record has no identity")
        return entry["id"]
    if surface == "compression_manifest" and entry.get("record_id") and entry.get("engine_id"):
        return json.dumps([entry["record_id"], entry["engine_id"], entry.get("engine_version")])
    if entry.get("id") is not None:
        return str(entry["id"])
    return hashlib.sha256(json.dumps(entry, sort_keys=True).encode()).hexdigest()


def write_export(pages, output, *, jsonl=False, include_sidecars=True):
    """Stage bounded pages on disk and publish only a complete validated file."""
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".mnemos-export-", dir=output.parent) as temporary:
        with sqlite3.connect(Path(temporary) / "spool.db") as db:
            db.execute("CREATE TABLE items (surface TEXT, identity TEXT, payload TEXT, PRIMARY KEY(surface,identity))")
            metadata = None
            complete = False
            wire_count = 0
            pending_entries = 0
            for page in pages:
                if page.get("export_complete") is True:
                    if page.get("record_count") != wire_count:
                        raise ExportValidationError("export completion count does not match received records")
                    complete = True
                    break
                records = page.get("records")
                if not isinstance(records, list):
                    raise ExportValidationError("export page has no records array")
                if metadata is None:
                    metadata = {
                        k: v
                        for k, v in page.items()
                        if k
                        not in (
                            "records",
                            "record_count",
                            "deletion_log_next_cursor",
                            *SIDECARS,
                        )
                    }
                new_records = 0
                wire_count += len(records)
                for surface in ("records", *SIDECARS) if include_sidecars else ("records",):
                    entries = page.get(surface, [])
                    if entries is None:
                        continue
                    if not isinstance(entries, list):
                        raise ExportValidationError(f"export {surface} must be an array")
                    pending_entries += len(entries)
                    for entry in entries:
                        if not isinstance(entry, dict):
                            raise ExportValidationError(f"export {surface} entry must be an object")
                        identity = _identity(surface, entry)
                        serialized = json.dumps(entry, sort_keys=True, ensure_ascii=False)
                        prior = db.execute(
                            "SELECT payload FROM items WHERE surface=? AND identity=?",
                            (surface, identity),
                        ).fetchone()
                        if prior:
                            if prior[0] != serialized:
                                raise ExportValidationError(f"divergent duplicate {surface} identity: {identity}")
                        else:
                            db.execute(
                                "INSERT INTO items VALUES (?,?,?)",
                                (surface, identity, serialized),
                            )
                            if surface == "records":
                                new_records += 1
                if records and not new_records:
                    raise ExportValidationError("export records did not advance")
                # Per-record framing must not turn one page commit into
                # thousands of fsyncs. The spool stays private until validated.
                if pending_entries >= 1000:
                    db.commit()
                    pending_entries = 0
            if not complete:
                raise ExportValidationError("export is incomplete")
            db.commit()
            count = db.execute("SELECT COUNT(*) FROM items WHERE surface='records'").fetchone()[0]
            staged = Path(temporary) / "output"
            with staged.open("w", encoding="utf-8") as target:

                def array(surface):
                    target.write("[")
                    comma = ""
                    for (payload,) in db.execute(
                        "SELECT payload FROM items WHERE surface=? ORDER BY rowid",
                        (surface,),
                    ):
                        target.write(comma + payload)
                        comma = ","
                    target.write("]")

                populated = [
                    name
                    for name in SIDECARS
                    if db.execute("SELECT 1 FROM items WHERE surface=? LIMIT 1", (name,)).fetchone()
                ]
                if jsonl:
                    for (payload,) in db.execute("SELECT payload FROM items WHERE surface='records' ORDER BY rowid"):
                        target.write(payload + "\n")
                    if populated:
                        target.write('{"mpf_sidecars":true')
                        for name in populated:
                            target.write("," + json.dumps(name) + ":")
                            array(name)
                        target.write("}\n")
                else:
                    target.write("{")
                    for name, value in (metadata or {}).items():
                        target.write(json.dumps(name) + ":" + json.dumps(value) + ",")
                    target.write(f'"record_count":{count},"records":')
                    array("records")
                    for name in populated:
                        target.write("," + json.dumps(name) + ":")
                        array(name)
                    target.write("}\n")
            os.replace(staged, output)
            return count


class DiskMemories:
    """Reiterable flat records; disk lifetime ends with close/context exit."""

    def __init__(self, pages):
        self._temporary = tempfile.TemporaryDirectory(prefix="mnemos-flat-export-")
        self.path = Path(self._temporary.name) / "records.jsonl"
        try:
            # Flat formats consume memory records only. Spooling sidecars would
            # emit one corpus-sized JSONL line and __iter__ would parse it all.
            write_export(pages, self.path, jsonl=True, include_sidecars=False)
            self.count = sum(1 for _ in self)
        except BaseException:
            self.close()
            raise

    def __iter__(self):
        with self.path.open(encoding="utf-8") as source:
            for line in source:
                record = json.loads(line)
                if record.get("kind") == "memory":
                    memory = dict(record.get("payload") or {})
                    memory.setdefault("id", record["id"])
                    yield memory

    def __len__(self):
        return self.count

    def close(self):
        self._temporary.cleanup()
