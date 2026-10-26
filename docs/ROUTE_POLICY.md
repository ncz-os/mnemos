# Route policy — banned provider/model pairs

Some provider/model pairs can never be served, and the gateway now refuses
them locally instead of spending a round trip discovering that upstream. The
single source of truth is `mnemos/domain/route_policy.py`:
`check_route(provider, model_id)` returns `None` when a pair may be
dispatched, or a reason string naming the rule that bans it.

## Rules

| Rule id | What it bans |
| --- | --- |
| `open-weight-only-provider` | A provider that carries only open-weight models may be routed open-weight models only (Nemotron and other open-weight families). The closed-weight route, the closed-weight route of an open-weight lab, and any third-party model the hub does not actually carry are refused. |
| `deepseek-direct-only` | DeepSeek models are dispatched only through the DeepSeek direct provider. |
| `minimax-direct-only` | MiniMax models are dispatched only through MiniMax direct. |
| `no-frontier-resale` | Closed-frontier models (`gpt-*`, `claude-*`, `gemini-*`, `grok-*`) are never routed through a reseller or a cloud marketplace route. |

Provider families and model patterns are data in that module (`RULES` and
the family sets beside it), not per-call conditionals. Model ids are folded
before matching: case is ignored, a `vendor/` gateway namespace is dropped
(`<open-weight-hub>/gpt-5.5` -> `gpt-5.5`), and access-tier / revision
suffixes such as `:free` or `-latest` are ignored. A provider or model this
module knows nothing about is always allowed, so an operator-added provider
keeps its existing behaviour.

Provider ids are matched on the family, which is the id's leading run of
segments. A deployment-flavoured spelling of a reseller — `aws-bedrock-xyz`,
`deepinfra-prod`, `openrouter-free`, `groq-eu` — is the same family as the
bare name and gets the same ban; suffixing an id can never be used to reach
a route the bare id is barred from. Matching is anchored at the head, so a
family token merely *contained* in an operator's own id (`my-groq-proxy`,
`acme-bedrock-lab`) does not match and keeps its current behaviour.

The set of models a provider that carries only open-weight models may serve
is an allowlist of open-weight families (`nemotron`, `llama`, `qwen`, `kimi`,
`mistral`, ...). A model the hub does not carry — including a third-party
model the hub resells without owning — is refused there, because that pair
is exactly the case where a hub publishes a borrowed id that it may stop
serving.

## Choke points

The check runs before any HTTP client is constructed:

- `mnemos/domain/openai_compat/providers.py::_prepare_provider_route` — the
  route-preparation step shared by blocking dispatch
  (`_route_to_provider_response`), streaming dispatch
  (`...streaming._route_to_provider_stream`) and `chat_completion`. A banned
  pair raises `OpenAICompatError` with status 400, `type:
  invalid_request_error`, `code: route_policy_violation`, and the rule id in
  the message.
- `mnemos/core/recommendation.py::choose_recommended_model` — PANTHEON's
  catalog, so the router never recommends a pair it would then refuse.
- `mnemos/domain/openai_compat/router.py::list_models` — model discovery, so
  a banned pair is never advertised as `available`.

Callback-style dispatch that binds a provider's model at construction time
bypasses route preparation entirely; it is unaffected, and no such pair
exists in the engine's own defaults (a test asserts this).
