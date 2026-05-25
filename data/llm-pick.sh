#!/usr/bin/env bash
# llm-pick — agent helper to query the fleet LLM registry
# Usage:
#   llm-pick                      # full registry summary
#   llm-pick tier B_fanout        # ranked picks for a tier
#   llm-pick use zeroclaw_fanout_routine
#   llm-pick provider deepseek_direct
#   llm-pick model deepseek_direct deepseek-v4-pro
#   llm-pick recommend <tier> <workload> [priority]
#
# Registry sources (probed in order, first existing wins):
#   1. $LLM_REGISTRY_PATH (env override)
#   2. /mnt/argonas/datapool/projects/fleet-registry/llm_provider_registry.json (NFS)
#   3. ~/.local/share/llm-registry/llm_provider_registry.json (local copy)
#   4. curl https://gitlab.com/ncz-os/mnemos/-/raw/feat/db2-native/data/llm_provider_registry.json (last resort)

set -uo pipefail

REG=""
for p in \
  "${LLM_REGISTRY_PATH:-}" \
  "/mnt/argonas/datapool/projects/fleet-registry/llm_provider_registry.json" \
  "$HOME/.local/share/llm-registry/llm_provider_registry.json"
do
  if [ -n "$p" ] && [ -r "$p" ]; then REG="$p"; break; fi
done

if [ -z "$REG" ]; then
  TMP=$(mktemp /tmp/llm-registry-XXXX.json)
  if curl -sSf -m 10 https://gitlab.com/ncz-os/mnemos/-/raw/feat/db2-native/data/llm_provider_registry.json -o "$TMP" 2>/dev/null; then
    REG="$TMP"
  else
    echo "ERROR: no registry source available (NFS + git both failed)" >&2
    exit 2
  fi
fi

cmd="${1:-summary}"
shift 2>/dev/null || true

case "$cmd" in
  summary|"")
    python3 -c "
import json,sys
d=json.load(open('$REG'))
print(f\"registry v{d['schema_version']} updated {d['updated_at']}\")
print(f\"source: $REG\")
print(f\"providers ({len(d['providers'])}): {', '.join(d['providers'].keys())}\")
print(f\"tier_picks: {', '.join(d['tier_picks'].keys())}\")
print(f\"use_cases: {', '.join(d['use_case_routing'].keys())}\")
"
    ;;
  tier)
    tier="${1:-}"
    [ -z "$tier" ] && { echo "usage: llm-pick tier <tier_name>"; exit 1; }
    python3 -c "
import json,sys
d=json.load(open('$REG'))
picks=d['tier_picks'].get('$tier',[])
if not picks:
    print(f'no picks for tier $tier; available: {list(d[\"tier_picks\"].keys())}'); sys.exit(1)
for rank,id in enumerate(picks,1):
    prov,model=id.split('.',1)
    m=d['providers'][prov]['models'].get(model,{})
    base=d['providers'][prov].get('base_url','-')
    cin=m.get('cost_in_per_m','?')
    cout=m.get('cost_out_per_m','?')
    print(f'{rank}. {id}  in=\${cin}/M out=\${cout}/M  base={base}')
"
    ;;
  use)
    uc="${1:-}"
    [ -z "$uc" ] && { echo "usage: llm-pick use <use_case>"; exit 1; }
    python3 -c "
import json,sys
d=json.load(open('$REG'))
r=d['use_case_routing'].get('$uc')
if not r:
    print(f'no routing for $uc; available: {list(d[\"use_case_routing\"].keys())}'); sys.exit(1)
print(json.dumps(r,indent=2))
"
    ;;
  provider)
    prov="${1:-}"
    [ -z "$prov" ] && { echo "usage: llm-pick provider <name>"; exit 1; }
    python3 -c "
import json,sys
d=json.load(open('$REG'))
p=d['providers'].get('$prov')
if not p: print(f'unknown; available: {list(d[\"providers\"].keys())}'); sys.exit(1)
print(json.dumps({k:v for k,v in p.items() if k!='models'},indent=2))
print(f'models ({len(p[\"models\"])}): {list(p[\"models\"].keys())}')
"
    ;;
  model)
    prov="${1:-}"; mod="${2:-}"
    [ -z "$prov" ] || [ -z "$mod" ] && { echo "usage: llm-pick model <provider> <model_id>"; exit 1; }
    python3 -c "
import json,sys
d=json.load(open('$REG'))
m=d['providers'].get('$prov',{}).get('models',{}).get('$mod')
if not m: print('not found'); sys.exit(1)
prov_info=d['providers']['$prov']
print(f'provider: $prov')
print(f'base_url: {prov_info.get(\"base_url\")}')
print(f'auth_env: {prov_info.get(\"auth_env\")}')
print(json.dumps(m,indent=2))
"
    ;;
  recommend)
    tier="${1:-B_fanout}"
    workload="${2:-fanout}"
    priority="${3:-MED}"
    # Look up tier first pick. If env-blocked (no key) try next.
    python3 -c "
import json,os,sys
d=json.load(open('$REG'))
candidates=d['tier_picks'].get('$tier',[])
for c in candidates:
    prov,model=c.split('.',1)
    auth=d['providers'][prov].get('auth_env')
    if auth and not os.environ.get(auth):
        continue  # skip if no key
    print(c)
    sys.exit(0)
print('NONE_AVAILABLE',file=sys.stderr); sys.exit(1)
"
    ;;
  list-providers)
    python3 -c "import json; [print(k) for k in json.load(open('$REG'))['providers'].keys()]"
    ;;
  list-tiers)
    python3 -c "import json; [print(k) for k in json.load(open('$REG'))['tier_picks'].keys()]"
    ;;
  list-use-cases)
    python3 -c "import json; [print(k) for k in json.load(open('$REG'))['use_case_routing'].keys()]"
    ;;
  raw)
    cat "$REG"
    ;;
  help|--help|-h)
    sed -n '2,12p' "$0"
    ;;
  *)
    echo "unknown cmd: $cmd. try: summary tier use provider model recommend list-providers list-tiers list-use-cases raw help"
    exit 1
    ;;
esac
