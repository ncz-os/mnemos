# GRAEAE OAuth shim

A tiny host-side HTTP shim that lets GRAEAE consult the **OpenAI** and
**Anthropic** muses through their **subscription OAuth CLIs** (`codex` /
`claude`) instead of metered API keys. The GRAEAE engine only speaks
HTTP+key, so this bridges that to the OAuth CLIs.

## Why

GRAEAE muses are HTTP providers (`[graeae.providers.<name>]` → `url` + a
`key_name`). Subscription OAuth for OpenAI/Anthropic lives in the `codex` /
`claude` CLIs, not in a static key. This shim exposes:

- `POST /openai/v1/chat/completions` → wraps `codex` (ChatGPT subscription OAuth)
- `POST /anthropic/v1/messages` → wraps `claude -p` (Claude subscription OAuth)
- `GET /health`

It binds `127.0.0.1:5079` and runs as the user that owns the OAuth creds
(`~/.codex/auth.json`, `~/.claude/.credentials.json`), passing `HOME` through so
the CLIs find them. **No vendor API keys** are used or stored.

## Deploy

The mnemos API container must be able to reach the host loopback — run it
`--network host` (the standard mnemos-api unit already does), so
`127.0.0.1:5079` from inside the container reaches this shim.

```bash
sudo install -D -m 0755 shim.py /opt/graeae-oauth-shim/shim.py
sudo install -m 0644 graeae-oauth-shim.service /etc/systemd/system/graeae-oauth-shim.service
# edit User=/HOME= in the unit to the account holding the OAuth creds
sudo systemctl daemon-reload
sudo systemctl enable --now graeae-oauth-shim.service
curl -fsS http://127.0.0.1:5079/health
```

Prereqs on the host: `codex` (logged in: `codex login`) and `claude`
(`npm i -g @anthropic-ai/claude-code`; auth via `claude setup-token` →
`CLAUDE_CODE_OAUTH_TOKEN`, or an interactive login). `claude` model defaults to
`claude-opus-4-8` (see `CLAUDE_MODEL` in `shim.py`).

## Point the muses at it

In the deployed `config.toml`, route the OpenAI/Anthropic muses through the
shim with a dummy `key_name` (the shim ignores inbound auth):

```toml
[graeae.providers.openai]
url       = "http://127.0.0.1:5079/openai/v1/chat/completions"
model     = "gpt-5.5"
api       = "openai"
key_name  = "oauth_shim"   # dummy entry in api_keys.json llm_providers
enabled   = true

[graeae.providers.claude]
url       = "http://127.0.0.1:5079/anthropic/v1/messages"
model     = "claude-opus-4-8"
api       = "anthropic"
key_name  = "oauth_shim"
enabled   = true
```

Add a dummy `llm_providers.oauth_shim = {"api_key": "local-oauth-shim"}` to
`api_keys.json` so `get_key()` resolves non-empty.

> Note: GRAEAE `mode` (local/external) is a quorum label, **not** a provider
> scope filter — to run external-only, disable the local-GPU provider blocks
> (`cerberus-*`, `hydra-*`, `achilles-*`) rather than relying on `mode`.
