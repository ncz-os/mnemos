# GRAEAE OAuth shim

The two inference routes require an Authorization bearer credential matching
GRAEAE_SHIM_TOKEN. Configure the same credential in the caller's oauth_shim
provider key. An unconfigured service does not start. Health returns only ok.

Install a dedicated graeae-shim system account and a minimal runtime root at
/var/lib/graeae-shim/root. Place Python, the selected CLI binaries and their
runtime libraries inside that root, with shim.py at
/opt/graeae-oauth-shim/shim.py. Create /var/lib/graeae-shim inside the runtime
root for service-only configuration and CLI authentication. Provision dedicated
CLI authentication there; do not bind-mount an operator home, general host
configuration or application data. The supplied unit requires this root and
runs with no capabilities, a read-only system and bounded tasks and memory.

Store GRAEAE_SHIM_TOKEN in /etc/graeae-shim/service.env on the host, readable
only by the service administrator. Install the supplied unit in the system
manager and start it only after the isolated runtime has been provisioned.
The HTTP listener is 127.0.0.1:5079. Configure callers to reach that loopback
interface and send the service credential.

Only the models named by CODEX_MODEL and CLAUDE_MODEL in shim.py are accepted.
The selected model is passed to the CLI. Streaming and unsupported execution
controls return a client error. Claude tools and additional MCP configuration
are disabled; Codex shell tools are disabled and its sandbox is read-only.
Both execute within the service unit's isolated filesystem root. Subprocesses
receive only the explicit service environment. Requests, output, concurrency
and process lifetime have fixed bounds; requests are not retried automatically.

Validate the runtime root and installed CLI versions in an isolated deployment
before rollout. Offline tests do not establish live CLI or systemd behavior.
