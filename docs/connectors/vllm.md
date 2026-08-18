# vLLM -> MNEMOS

vLLM can use MNEMOS through its OpenAI-compatible tool-call API when served with automatic tool choice and the correct per-model parser.

## What you need — token, host (<mnemos-host>), relevant port(s)

- vLLM installed on the model-serving host.
- A model with reliable tool-call training.
- `--enable-auto-tool-choice` enabled on the vLLM server.
- The right `--tool-call-parser` for the model family.
- Python 3.11+ with the OpenAI SDK.
- MNEMOS MCP HTTP/SSE reachable at `http://<mnemos-host>:5003/sse`.
- A MNEMOS bearer token exported as `MNEMOS_TOKEN`.
- The `mnemos-bridge-openai` adapter.
- vLLM's OpenAI-compatible endpoint reachable at `http://localhost:8000/v1`,
  or whichever endpoint you expose.

## Configuration snippet — vLLM serve plus OpenAI SDK client

Start vLLM with automatic tool choice and a parser that matches the loaded
model family:

```bash
vllm serve <model> \
  --host 0.0.0.0 \
  --port 8000 \
  --enable-auto-tool-choice \
  --tool-call-parser <parser>
```

Parser choices are model-family specific:

- `hermes` for the NousResearch/Hermes family.
- `mistral` for the Mistral family.
- `llama3_json` for the Meta-Llama-3 family.

Fleet live example: <gpu-host> at `<host>:8000` runs vLLM with
Mistral-7B and `--tool-call-parser mistral`.

Then use the OpenAI SDK pattern and list MNEMOS tools through the bridge
adapter:

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

client = OpenAI(base_url="http://localhost:8000/v1", api_key="vllm")
response = client.chat.completions.create(
    model="<model>",
    messages=[{"role": "user", "content": "Search MNEMOS for vLLM notes."}],
    tools=mnemos.tools(),
    tool_choice="auto",
)
print(mnemos.dispatch_tool_calls(response))
```

For the <gpu-host> fleet endpoint, replace the client base URL with
`http://<host>:8000/v1`.

## Verification — one curl or one tool-list call that proves registration worked

```bash
curl -fsS http://localhost:8000/v1/models
curl -fsS -H "Authorization: Bearer $MNEMOS_TOKEN" http://<mnemos-host>:5003/sse
```

The first command proves the vLLM OpenAI-compatible server is up. The second
authenticated request should open a `text/event-stream` from MNEMOS.

## Common gotchas — 2-4 bullets of real failure modes

- `--enable-auto-tool-choice` is required; without it vLLM ignores the
  `tools` parameter silently.
- The parser must match the model family; a mismatched parser can produce
  malformed tool calls.
- vLLM does not speak MCP directly; use the OpenAI-compatible bridge path.
- Remote fleet endpoints should keep MNEMOS bearer tokens out of shared shell
  history and notebooks.
