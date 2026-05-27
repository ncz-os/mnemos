"""mnemos/llm.py — Knemon LLM wrapper (groq/xai tier)

Minimal per spec: pantheon.route + provider_registry + ledger finally.
"""

from mnemos.pantheon import route as pantheon_route
from mnemos.providers.registry import invoke as provider_invoke
from mnemos.ledger import ledger


def call(task):
    """Route task, invoke, always record to ledger even on exception."""
    model = pantheon_route(task)
    try:
        res = provider_invoke(model, task)
    finally:
        ledger.record(model=model, task=task, result=locals().get('res'))
    return res
