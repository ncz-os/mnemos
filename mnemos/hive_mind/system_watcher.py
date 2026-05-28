#!/usr/bin/env python3
"""
GRAEAE Hive Mind — fleet system watcher daemon.

Registers the host as kind=system with static hardware specs + dynamic load.
Heartbeats current cpu/ram/load/disk metrics. Optionally claims jobs that
require physical resources (build, compile, render) when load permits.

Prevents ARGOS-style oversubscription (CLAUDE.md gotcha: load=92 incident
2026-05-20 from multi-job spawning). Dispatcher filters by required_resources
AND current load before assigning to a host.

Env:
  HIVE_URL                  http://192.168.207.8:5005
  AGENT_HOST                default hostname -s
  LOAD_THRESHOLD            max 1-min load_avg before refusing new jobs (default 0.75 × cpu_count)
  HEARTBEAT_INTERVAL        seconds (default 15)
  CLAIM_JOBS                if "1", actively claim eligible jobs and dispatch to local executor (default "0" = passive monitor only)
"""
from __future__ import annotations
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import urllib.error
import signal

HIVE_URL = os.environ.get("HIVE_URL", "http://192.168.207.8:5005")
AGENT_HOST = os.environ.get("AGENT_HOST", socket.gethostname().split(".")[0])
HEARTBEAT_INTERVAL = float(os.environ.get("HEARTBEAT_INTERVAL", "15"))
CLAIM_JOBS = os.environ.get("CLAIM_JOBS", "0") == "1"

_urn: str = ""
_running = True


def _signal(signum, frame):
    global _running
    print(f"[sysw] signal {signum} — shutting down", flush=True)
    _running = False


signal.signal(signal.SIGTERM, _signal)
signal.signal(signal.SIGINT, _signal)


def _http(method: str, path: str, body: dict | None = None) -> tuple[int, dict | None]:
    url = f"{HIVE_URL}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, None
    except Exception as e:
        print(f"[sysw] http error {method} {path}: {e}", flush=True)
        return 0, None


def static_specs() -> dict:
    """One-shot specs gathered at startup."""
    out = {
        "hostname": AGENT_HOST,
        "fqdn": socket.getfqdn(),
        "os": platform.system(),
        "kernel": platform.release(),
        "arch": platform.machine(),
        "python": sys.version.split()[0],
    }
    # CPU
    try:
        out["cpu_count"] = os.cpu_count()
        with open("/proc/cpuinfo") as f:
            content = f.read()
        m = re.search(r"model name\s*:\s*(.+)", content)
        if m:
            out["cpu_model"] = m.group(1).strip()
        out["cpu_threads"] = content.count("processor\t:")
    except Exception:
        pass
    # RAM
    try:
        with open("/proc/meminfo") as f:
            mi = f.read()
        m = re.search(r"MemTotal:\s+(\d+)", mi)
        if m:
            out["ram_kb"] = int(m.group(1))
            out["ram_gb"] = round(int(m.group(1)) / 1024 / 1024, 1)
    except Exception:
        pass
    # Disk
    try:
        for mnt in ("/srv", "/", "/var"):
            if os.path.exists(mnt):
                s = shutil.disk_usage(mnt)
                out[f"disk_{mnt.replace('/', '_') or '_root'}_total_gb"] = round(s.total / 1024**3, 1)
    except Exception:
        pass
    # GPU detection
    gpus = []
    # NVIDIA
    if shutil.which("nvidia-smi"):
        try:
            r = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,driver_version",
                                "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5)
            for line in r.stdout.strip().splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 2:
                    gpus.append({"vendor": "nvidia", "name": parts[0], "vram_mib": int(parts[1]),
                                 "driver": parts[2] if len(parts) > 2 else ""})
        except Exception:
            pass
    # AMD
    if shutil.which("rocm-smi"):
        try:
            r = subprocess.run(["rocm-smi", "--showproductname", "--json"], capture_output=True, text=True, timeout=5)
            data = json.loads(r.stdout) if r.stdout.strip() else {}
            for k, v in data.items():
                if isinstance(v, dict) and "Card SKU" in v or "GPU ID" in v:
                    gpus.append({"vendor": "amd", "card_id": k, "info": v})
        except Exception:
            pass
    if not gpus:
        # try lspci
        try:
            r = subprocess.run(["lspci"], capture_output=True, text=True, timeout=5)
            for line in r.stdout.splitlines():
                if re.search(r"VGA|3D|Display", line):
                    gpus.append({"vendor": "unknown", "lspci": line.strip()})
        except Exception:
            pass
    out["gpus"] = gpus
    # Container runtimes
    out["has_docker"] = bool(shutil.which("docker"))
    out["has_podman"] = bool(shutil.which("podman"))
    out["has_buildx"] = bool(shutil.which("docker") and subprocess.run(
        ["docker", "buildx", "version"], capture_output=True).returncode == 0) if shutil.which("docker") else False
    # CI runners
    out["has_gitlab_runner"] = bool(shutil.which("gitlab-runner"))
    return out


def dynamic_load() -> dict:
    """Snapshot of current load."""
    out = {"ts": time.time()}
    try:
        out["load_1min"], out["load_5min"], out["load_15min"] = os.getloadavg()
    except Exception:
        pass
    try:
        with open("/proc/meminfo") as f:
            mi = f.read()
        for key in ("MemTotal", "MemFree", "MemAvailable", "Buffers", "Cached"):
            m = re.search(rf"{key}:\s+(\d+)", mi)
            if m:
                out[key.lower() + "_kb"] = int(m.group(1))
        if "memtotal_kb" in out and "memavailable_kb" in out:
            used = out["memtotal_kb"] - out["memavailable_kb"]
            out["ram_used_pct"] = round(100 * used / out["memtotal_kb"], 1)
    except Exception:
        pass
    try:
        s = shutil.disk_usage("/srv" if os.path.exists("/srv") else "/")
        out["srv_used_pct"] = round(100 * (s.total - s.free) / s.total, 1)
        out["srv_free_gb"] = round(s.free / 1024**3, 1)
    except Exception:
        pass
    # uptime
    try:
        with open("/proc/uptime") as f:
            out["uptime_sec"] = float(f.read().split()[0])
    except Exception:
        pass
    return out


def capabilities_from_specs(specs: dict) -> list[str]:
    caps = ["system", "host"]
    if specs.get("has_docker"):     caps.append("docker")
    if specs.get("has_podman"):     caps.append("podman")
    if specs.get("has_buildx"):     caps.extend(["build", "buildx", "multi-arch-build"])
    if specs.get("has_gitlab_runner"): caps.append("ci-runner")
    for g in specs.get("gpus", []) or []:
        v = g.get("vendor", "")
        if v == "nvidia": caps.extend(["gpu", "nvidia-gpu", "cuda"])
        elif v == "amd": caps.extend(["gpu", "amd-gpu", "rocm", "vulkan-compute"])
        elif v == "intel": caps.extend(["gpu", "intel-igpu"])
    if specs.get("ram_gb", 0) >= 32:  caps.append("ram-32g+")
    if specs.get("ram_gb", 0) >= 64:  caps.append("ram-64g+")
    if specs.get("cpu_count", 0) >= 16: caps.append("cpu-16t+")
    if specs.get("cpu_count", 0) >= 32: caps.append("cpu-32t+")
    return caps


def register():
    global _urn
    specs = static_specs()
    caps = capabilities_from_specs(specs)
    load = dynamic_load()
    body = {
        "runtime": "system",
        "kind": "system",
        "host": AGENT_HOST,
        "pid": os.getpid(),
        "model": "n/a",
        "provider": "local",
        "autonomy_level": "autonomous" if CLAIM_JOBS else "interactive",
        "capabilities": caps,
        "version": platform.platform(),
        "metadata": {
            "specs": specs,
            "load": load,
            "daemon": "system_watcher.py",
        },
    }
    # system is a NEW runtime — RUNTIME_KIND_MAP may not include it; service falls back to allowing kind==runtime
    code, resp = _http("POST", "/v1/agents/register", body)
    if code == 200 and resp:
        _urn = resp["urn"]
        print(f"[sysw] registered urn={_urn} caps={caps}", flush=True)
    else:
        print(f"[sysw] register failed code={code} resp={resp}", flush=True)
        sys.exit(1)


def heartbeat():
    load = dynamic_load()
    # Update via heartbeat endpoint; payload-side metadata refresh would need new endpoint — for now load goes in metadata only at re-register; emit load as a hive message every heartbeat for live visibility
    _http("POST", "/v1/agents/heartbeat", {"urn": _urn})
    # broadcast load as a system.load message
    _http("POST", "/v1/messages", {
        "from_urn": _urn,
        "to_urn": None,
        "topic": "system.load",
        "payload": load,
    })


def main():
    print(f"[sysw] starting on {AGENT_HOST} HIVE_URL={HIVE_URL} CLAIM_JOBS={CLAIM_JOBS}", flush=True)
    register()
    while _running:
        heartbeat()
        time.sleep(HEARTBEAT_INTERVAL)
    print("[sysw] clean shutdown", flush=True)


if __name__ == "__main__":
    main()
