# LM Studio -> MNEMOS

LM Studio has no native MCP client, so use its OpenAI-compatible local server
with a tool-aware model plus the
[`mnemos-bridge-openai`](https://gitlab.com/ncz-os/mnemos-bridge-openai) adapter.

## What you need — token, host (<mnemos-host>), relevant port(s)

- LM Studio installed with the local server enabled.
- A model loaded in LM Studio with tool-call support enabled.
- Python 3.11+ with the OpenAI SDK.
- MNEMOS MCP HTTP/SSE reachable at `http://<mnemos-host>:5003/sse`.
- A MNEMOS bearer token exported as `MNEMOS_TOKEN`.
- The `mnemos-bridge-openai` adapter.
- LM Studio's OpenAI-compatible endpoint reachable at
  `http://localhost:1234/v1`.
- A GGUF quantisation that preserves tool schemas.
- A private shell environment for the token.

## Configuration snippet — OpenAI SDK against LM Studio

Start LM Studio's local server, load a tool-aware instruct model, then point a
Python client at the default OpenAI-compatible base URL. The bridge adapter
lists MNEMOS tools from the HTTP/SSE endpoint and dispatches tool calls back
to MNEMOS.

```bash
python -m pip install openai mnemos-bridge-openai
```

```python
import os
from openai import OpenAI
from mnemos_bridge_openai import MnemosOpenAITools

mnemos = MnemosOpenAITools.from_sse(
    url="http://<mnemos-host>:5003/sse",
    headers={"Authorization": f"Bearer {os.environ['MNEMOS_TOKEN']}"},
)

client = OpenAI(base_url="http://localhost:1234/v1", api_key="lm-studio")
response = client.chat.completions.create(
    model="local-model",
    messages=[{"role": "user", "content": "Search MNEMOS for LM Studio notes."}],
    tools=mnemos.tools(),
    tool_choice="auto",
)
print(mnemos.dispatch_tool_calls(response))
```

LM Studio's tool-call support is per-model. Prefer Q4_K_M or higher
quantisations for reliable tool-call support; some smaller GGUF
quantisations disable or degrade tool schemas.

## Verification — one curl or one tool-list call that proves registration worked

```bash
curl -fsS http://localhost:1234/v1/models
curl -fsS -H "Authorization: Bearer $MNEMOS_TOKEN" http://<mnemos-host>:5003/sse
```

The first command proves LM Studio's local server is serving an
OpenAI-compatible model. The second authenticated request should open a
`text/event-stream` from MNEMOS.

## Common gotchas — 2-4 bullets of real failure modes

- LM Studio does not speak MCP directly; use the OpenAI-compatible tool-call
  path.
- Tool-call support is per-model; some GGUF quantisations disable tool schemas.
- The `mnemos-bridge-openai` adapter provides a uniform experience across the
  OpenAI-compatible surfaces.
- Keep write tools behind explicit approval in shared local-server setups.
