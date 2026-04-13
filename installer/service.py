"""Service installation for systemd (Linux) and launchd (macOS)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .wizard import Config


def _run(cmd: list[str], timeout: int = 30) -> tuple[int, str, str]:
    """Run a command, return (returncode, stdout, stderr). Never raises."""
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception as exc:
        return 1, "", str(exc)


def create_service_user(username: str) -> bool:
    """Create a system user for the service. Idempotent. Linux only."""
    if sys.platform == "darwin":
        print(f"[service] macOS: skipping system user creation for '{username}'")
        return True

    # Check if user already exists
    rc, _, _ = _run(["id", username])
    if rc == 0:
        print(f"[service] User '{username}' already exists.")
        return True

    print(f"[service] Creating system user '{username}'...")
    rc, _, err = _run([
        "sudo", "/usr/sbin/useradd",
        "--system",
        "--no-create-home",
        "--shell", "/usr/sbin/nologin",
        username,
    ])
    if rc != 0:
        print(f"[service] ERROR creating user '{username}': {err}", file=sys.stderr)
        return False
    print(f"[service] User '{username}' created.")
    return True


def _write_env_file(config: Config, env_path: str) -> bool:
    """Write environment variables to /etc/mnemos/mnemos.env."""
    env_dir = Path(env_path).parent
    try:
        # /etc/mnemos requires root — try sudo mkdir first
        if not env_dir.exists():
            rc, _, err = _run(["sudo", "mkdir", "-p", str(env_dir)])
            if rc != 0:
                # Last resort: try direct (may work if running as root)
                env_dir.mkdir(parents=True, exist_ok=True)

        lines = [
            "# MNEMOS environment — managed by installer",
            f"PG_HOST={config.db_host}",
            f"PG_PORT={config.db_port}",
            f"PG_DATABASE={config.db_name}",
            f"PG_USER={config.db_user}",
            f"PG_PASSWORD={config.db_password}",
            f"MNEMOS_LISTEN_PORT={config.listen_port}",
            f"MNEMOS_SERVICE_USER={config.service_user}",
            f"OLLAMA_EMBED_HOST={config.ollama_embed_host}",
        ]
        for provider, key in config.graeae_providers.items():
            lines.append(f"{provider.upper()}_API_KEY={key}")

        import tempfile
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".mnemos.env", delete=False
        ) as _tf:
            _tf.write("\n".join(lines) + "\n")
            _tmp = _tf.name
        # /etc/mnemos requires root; sudo-move the temp file in
        _run(["sudo", "mv", _tmp, env_path])

        # Restrict permissions — contains DB password
        os.chmod(env_path, 0o640)

        # chown to root:service_user if not root
        if os.getuid() == 0:
            import grp
            try:
                gid = grp.getgrnam(config.service_user).gr_gid
                os.chown(env_path, 0, gid)
            except KeyError:
                pass

        return True
    except Exception as exc:
        print(f"[service] ERROR writing env file: {exc}", file=sys.stderr)
        return False


def install_systemd(config: Config, repo_path: str) -> bool:
    """Write systemd unit file and environment file. Return True on success."""
    repo = Path(repo_path)
    venv_python = repo / "venv" / "bin" / "python"
    api_server = repo / "api_server.py"
    service_path = "/etc/systemd/system/mnemos.service"
    env_path = "/etc/mnemos/mnemos.env"

    print("[service] Installing systemd service...")

    # Write environment file first
    if not _write_env_file(config, env_path):
        return False

    unit_content = f"""[Unit]
Description=MNEMOS Memory System API
Documentation=https://github.com/mnemos-ai/mnemos
After=network.target postgresql.service
Wants=postgresql.service

[Service]
Type=simple
User={config.service_user}
Group={config.service_user}
WorkingDirectory={repo_path}
EnvironmentFile={env_path}
ExecStart={venv_python} {api_server}
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=mnemos

# Hardening
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ReadWritePaths={repo_path} /etc/mnemos /tmp

[Install]
WantedBy=multi-user.target
"""

    try:
        import tempfile
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".mnemos.service", delete=False
        ) as _tf:
            _tf.write(unit_content)
            _tmp = _tf.name
        rc_mv, _, err_mv = _run(["sudo", "mv", _tmp, service_path])
        if rc_mv != 0:
            print(f"[service] ERROR: sudo mv failed: {err_mv}", file=sys.stderr)
            return False
        _run(["sudo", "chmod", "644", service_path])
        print(f"[service] Wrote {service_path}")
    except Exception as exc:
        print(f"[service] ERROR writing service file: {exc}", file=sys.stderr)
        return False

    # Reload daemon
    rc, _, err = _run(["sudo", "systemctl", "daemon-reload"])
    if rc != 0:
        print(f"[service] WARNING: daemon-reload failed: {err}", file=sys.stderr)

    return True


def install_launchd(config: Config, repo_path: str) -> bool:
    """Write launchd plist for macOS. Return True on success."""
    repo = Path(repo_path)
    venv_python = repo / "venv" / "bin" / "python"
    api_server = repo / "api_server.py"

    home = Path.home()
    launch_agents = home / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True, exist_ok=True)
    plist_path = launch_agents / "ai.mnemos.plist"

    print("[service] Installing launchd plist...")

    # Build env vars XML
    env_lines = []
    env_vars = {
        "PG_HOST": config.db_host,
        "PG_PORT": str(config.db_port),
        "PG_DB": config.db_name,
        "PG_USER": config.db_user,
        "PG_PASSWORD": config.db_password,
        "MNEMOS_LISTEN_PORT": str(config.listen_port),
        "OLLAMA_EMBED_HOST": config.ollama_embed_host,
    }
    for provider, key in config.graeae_providers.items():
        env_vars[f"{provider.upper()}_API_KEY"] = key

    for k, v in env_vars.items():
        env_lines.append(f"        <key>{k}</key>")
        env_lines.append(f"        <string>{v}</string>")

    env_block = "\n".join(env_lines)

    plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>ai.mnemos</string>

    <key>ProgramArguments</key>
    <array>
        <string>{venv_python}</string>
        <string>{api_server}</string>
    </array>

    <key>WorkingDirectory</key>
    <string>{repo_path}</string>

    <key>EnvironmentVariables</key>
    <dict>
{env_block}
    </dict>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <dict>
        <key>Crashed</key>
        <true/>
    </dict>

    <key>StandardOutPath</key>
    <string>{home}/Library/Logs/mnemos.log</string>

    <key>StandardErrorPath</key>
    <string>{home}/Library/Logs/mnemos.error.log</string>
</dict>
</plist>
"""

    try:
        with open(plist_path, "w") as fh:
            fh.write(plist_content)
        print(f"[service] Wrote {plist_path}")
        return True
    except Exception as exc:
        print(f"[service] ERROR writing plist: {exc}", file=sys.stderr)
        return False


def enable_service(service_name: str) -> bool:
    """Enable the service to start on boot."""
    if sys.platform == "darwin":
        plist_path = Path.home() / "Library" / "LaunchAgents" / f"{service_name}.plist"
        rc, _, err = _run(["launchctl", "load", "-w", str(plist_path)])
        if rc != 0:
            print(f"[service] WARNING enabling launchd service: {err}", file=sys.stderr)
            return False
        return True

    rc, _, err = _run(["sudo", "systemctl", "enable", service_name])
    if rc != 0:
        print(f"[service] WARNING enabling service: {err}", file=sys.stderr)
        return False
    return True


def start_service(service_name: str) -> bool:
    """Start the service now."""
    if sys.platform == "darwin":
        plist_path = Path.home() / "Library" / "LaunchAgents" / f"{service_name}.plist"
        rc, _, err = _run(["launchctl", "start", service_name])
        if rc != 0:
            print(f"[service] WARNING starting launchd service: {err}", file=sys.stderr)
            return False
        return True

    rc, _, err = _run(["sudo", "systemctl", "start", service_name])
    if rc != 0:
        print(f"[service] WARNING starting service: {err}", file=sys.stderr)
        return False
    return True


def service_status(service_name: str) -> str:
    """Return 'active', 'inactive', 'failed', or 'unknown'."""
    if sys.platform == "darwin":
        rc, out, _ = _run(["launchctl", "list", service_name])
        if rc == 0:
            return "active"
        return "inactive"

    if not _which_exists("systemctl"):
        return "unknown"

    rc, out, _ = _run(["systemctl", "is-active", service_name])
    state = out.strip().lower()
    if state in ("active", "inactive", "failed"):
        return state
    return "unknown"


def _which_exists(name: str) -> bool:
    rc, _, _ = _run(["which", name])
    return rc == 0
