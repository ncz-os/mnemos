# Zeroclaw fleet fan-out + multi-muse provider rollout

WIP — staged 2026-05-24. Designed but not yet fleet-deployed (cixmini POC
blocked on sudo-tmp perm; resume in follow-up session).

## Goals

1. **Auto fan-out per host** — each agent-pool node enables N concurrent
   `zeroclaw-worker@1..@N` instances. N derived from `min(cores//2,
   ram_gb//2, 8)`. Per-host cap overrides via
   `ZC_FANOUT_FORCE_N=N` in `/etc/default/zeroclaw-fanout`.

2. **All 8 GRAEAE muses available** — canonical `config.toml` with
   providers for groq, together, openai, claude, perplexity, xai, nvidia
   (NGC), gemini. 4 hive agents pre-configured: `hive` (Tier B default),
   `hive_tier_a` (Anthropic Opus), `hive_tier_b` (Groq Llama-3.3-70B),
   `hive_tier_c` (NGC Kimi K2.6 free).

3. **Per-tier agent dispatch** — workers register provider matching their
   tier capability. Tier semantics post-flip:
   - A = premium (Claude, GPT-5.2, Gemini Pro, Together MiniMax M2.7)
   - B = mid (Groq, xAI, cheap Together/OpenAI/Gemini, Perplexity)
   - C = routine (NGC free, local llama.cpp, Ollama)

4. **Metadata-aware registration** — workers send `cores`, `ram_mb`,
   `instance_id` to hive at registration. Operators see capacity per host.

## Files

| File | Install path | Notes |
|---|---|---|
| `config.toml.template` | `~/.zeroclaw/config.toml` | Substitute `__REPLACE_BY_DEPLOY_SCRIPT__` with keys from `~/.api_keys_master.json` |
| `zeroclaw_worker.py` | `~/zeroclaw_worker.py` (user home varies per host) | Patched shim with `_probe_cores`, `_probe_ram_mb`, `_instance_id` reporting |
| `zeroclaw-fanout-init.sh` | `/usr/local/sbin/zeroclaw-fanout-init` | Boot-time probe + enable @1..@N |
| `zeroclaw-fanout.service` | `/etc/systemd/system/zeroclaw-fanout.service` | One-shot at boot |
| `zeroclaw-worker@.service` | `/etc/systemd/system/zeroclaw-worker@.service` | Template w/ `INSTANCE=%i` env injection |

## Agent-pool roster (post 2026-05-24 directive)

Agent-pool hosts (run fan-out):

| Host | Cores | RAM | Expected N | User |
|---|---|---|---|---|
| cixmini | 12 | 62GB | 6 | mini |
| CERBERUS | 24 | 125GB | **4** (override — GPU contention) | jasonperlow |
| MEDUSA | 12 | 16GB | 6 | jasonperlow |
| PYTHIA | 12 | 30GB | 6 | jasonperlow |
| HYDRA | 8 | 32GB | 4 | jasonperlow |
| bigpi | 4 | 16GB | 2 | ncz |
| clawpi | 4 | 8GB | 2 | ncz |
| zeropi | 4 | 2GB | 1 | ncz |

Total expected: **31 worker instances** vs current 9.

**RESERVED hosts (NOT in agent pool, pipeline/build only)**:
- TYPHON — x86 + RTX 5060 build/CI
- ARGOS — gitlab-runner + internal apt mirror + RiskyEats render
- PROTEUS — batch ETL

These had zeroclaw-worker@1 stopped+disabled on 2026-05-24.

## Deploy procedure

For each agent-pool host:

```bash
# 1. Substitute keys into config
python3 -c "
import json
d=json.load(open('~/.api_keys_master.json'))
p=d['llm_providers']
K={'CLAUDE':p['anthropic']['api_key'], 'OPENAI':p['openai']['api_key'],
   'GEMINI':p['google_gemini']['api_key'], 'PERPLEXITY':p['perplexity']['api_key'],
   'GROQ':p['groq']['api_key'], 'TOGETHER':p['together_ai']['api_key'],
   'XAI':p['xai']['api_key'], 'NVIDIA':p['nvidia']['api_key']}
src=open('config.toml.template').read()
# Per-section substitution — template needs key-section pairing
# TODO: write proper subst script that maps placeholder by section
"

# 2. Install on host (per-host User= override)
sudo install -m 0640 config.toml ~/.zeroclaw/config.toml
sudo install -m 0755 zeroclaw_worker.py ~/zeroclaw_worker.py
sudo install -m 0755 zeroclaw-fanout-init.sh /usr/local/sbin/zeroclaw-fanout-init
sudo install -m 0644 zeroclaw-fanout.service /etc/systemd/system/
sudo install -m 0644 zeroclaw-worker@.service /etc/systemd/system/

# 3. CERBERUS override (cap at 4 due to GPU contention)
echo 'ZC_FANOUT_FORCE_N=4' | sudo tee /etc/default/zeroclaw-fanout

# 4. Enable + run
sudo systemctl daemon-reload
sudo systemctl stop zeroclaw-worker@1  # stop solo worker
sudo systemctl enable --now zeroclaw-fanout.service
```

## Open issues (POC blockers)

1. **Per-section key substitution script not written** — template has 20
   `__REPLACE_BY_DEPLOY_SCRIPT__` markers; need to map each by section
   (e.g. `[providers.models.claude.opus_4_6]` gets CLAUDE key).
2. **cixmini POC scp failed** — `/tmp` write-restricted; need alternate
   staging dir or sudo-from-stdin pattern.
3. **systemd unit `User=` per-host** — template can't use `%U`
   specifier; need 3 variants (mini, ncz, jasonperlow) or pre-process
   at deploy time.
4. **GRAEAE muse-DB drift check** (Phase 3, not in this commit) — shim
   should hit `http://192.168.207.67:5002/v1/consultations/muses` on
   startup and log warning if local config disagrees.

## Verification post-rollout

```bash
# Hive should see N agents per host
curl -sS http://192.168.207.67:5005/v1/agents | \
  python3 -c "import json,sys; d=json.load(sys.stdin); \
    from collections import Counter; \
    print(Counter(a['urn'].split(':')[3] for a in d.get('agents',d) \
      if a.get('urn','').startswith('urn:agent:zeroclaw:') and a.get('status')=='online'))"
```

Expected count: 31 online zeroclaw agents (vs current 9).
