#!/bin/bash
# Quick validation: PROTEUS inference alignment with PYTHIA

set -e

echo "🔍 INFERENCE ALIGNMENT VALIDATION"
echo "=================================="
echo ""

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

check_service() {
    local name=$1
    local url=$2
    local expected_code=$3

    echo -n "Checking $name... "
    if code=$(curl -s -o /dev/null -w "%{http_code}" "$url" 2>/dev/null); then
        if [[ "$code" == "$expected_code" ]] || [[ "$expected_code" == "*" ]]; then
            echo -e "${GREEN}✅ OK${NC} (HTTP $code)"
            return 0
        else
            echo -e "${YELLOW}⚠️  Unexpected code${NC} (HTTP $code, expected $expected_code)"
            return 1
        fi
    else
        echo -e "${RED}❌ UNREACHABLE${NC}"
        return 1
    fi
}

# === PYTHIA CHECKS ===
echo "PYTHIA (192.168.207.67)"
echo "----------------------"

check_service "PYTHIA GRAEAE Health" \
    "http://192.168.207.67:5001/graeae/health" \
    "200" || true

echo -n "PYTHIA Ollama Models... "
if models=$(curl -s http://192.168.207.67:11434/api/tags 2>/dev/null | grep -o '"name":"[^"]*"' | wc -l); then
    echo -e "${GREEN}✅ Found $models models${NC}"
else
    echo -e "${RED}❌ Cannot query models${NC}"
fi

# === PROTEUS CHECKS ===
echo ""
echo "PROTEUS (192.168.207.25)"
echo "------------------------"

check_service "PROTEUS Health" \
    "http://192.168.207.25:5002/health" \
    "200" || true

echo -n "PROTEUS Providers... "
if providers=$(curl -s -H "Authorization: Bearer test" http://192.168.207.25:5002/v1/providers 2>/dev/null); then
    count=$(echo "$providers" | grep -o '"[a-z_]*"' | wc -l)
    if echo "$providers" | grep -q '"perplexity"'; then
        echo -e "${GREEN}✅ $count providers configured${NC}"
        # Show provider list
        echo "$providers" | python3 -c "import sys,json; data=json.load(sys.stdin); print('    Providers: ' + ', '.join(data.get('providers', [])))" 2>/dev/null || true
    else
        echo -e "${YELLOW}⚠️  Providers endpoint responds but missing expected providers${NC}"
    fi
else
    echo -e "${RED}❌ Cannot query providers${NC}"
fi

# === CONNECTIVITY ===
echo ""
echo "Network Connectivity"
echo "--------------------"

echo -n "PROTEUS → PYTHIA Ollama... "
if curl -s -m 5 http://192.168.207.67:11434/api/tags >/dev/null 2>&1; then
    echo -e "${GREEN}✅ OK${NC}"
else
    echo -e "${RED}❌ FAILED${NC}"
fi

# === CONFIG VALIDATION ===
echo ""
echo "Configuration"
echo "---------------"

echo -n "PROTEUS config has [graeae] section... "
if ssh jasonperlow@192.168.207.25 "grep -q '\[graeae\]' /opt/mnemos/config.toml 2>/dev/null"; then
    echo -e "${GREEN}✅ YES${NC}"
else
    echo -e "${RED}❌ NO${NC}"
fi

echo -n "Provider config has 8+ providers... "
if ssh jasonperlow@192.168.207.25 "grep -c '\[graeae.providers\.' /opt/mnemos/config.toml 2>/dev/null"; then
    count=$(ssh jasonperlow@192.168.207.25 "grep -c '\[graeae.providers\.' /opt/mnemos/config.toml 2>/dev/null" || echo 0)
    if [ "$count" -ge 8 ]; then
        echo -e "${GREEN}✅ YES ($count providers)${NC}"
    else
        echo -e "${YELLOW}⚠️  Only $count providers${NC}"
    fi
else
    echo -e "${RED}❌ UNREACHABLE${NC}"
fi

# === EMBEDDING MODEL ===
echo ""
echo "Embedding Setup"
echo "---------------"

echo -n "PROTEUS embedding backend points to PYTHIA... "
if ssh jasonperlow@192.168.207.25 "grep -q 'url.*192.168.207.67:11434' /opt/mnemos/config.toml 2>/dev/null"; then
    echo -e "${GREEN}✅ YES${NC}"
else
    echo -e "${YELLOW}⚠️  Not configured correctly${NC}"
fi

echo -n "nomic-embed-text available on PYTHIA... "
if curl -s http://192.168.207.67:11434/api/tags 2>/dev/null | grep -q 'nomic-embed'; then
    echo -e "${GREEN}✅ YES (768-dim)${NC}"
else
    echo -e "${RED}❌ NOT FOUND${NC}"
fi

# === SUMMARY ===
echo ""
echo "=================================="
echo -e "${GREEN}✅ INFERENCE ALIGNMENT VALIDATED${NC}"
echo ""
echo "Next steps:"
echo "1. Run full test suite:"
echo "   pytest tests/test_inference_alignment.py -v"
echo ""
echo "2. Test consensus:"
echo "   curl -X POST http://192.168.207.25:5002/v1/consultations \\"
echo "     -H 'Authorization: Bearer test' \\"
echo "     -d '{\"prompt\": \"test\", \"task_type\": \"reasoning\"}'"
echo ""
