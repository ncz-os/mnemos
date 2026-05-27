"""mnemos/llm.py — knemon llm wrapper (sec5 design)"""
from mnemos.pantheon import route as pantheon_route
from mnemos.providers import registry as provider_registry
from mnemos.ledger import record as ledger_record

def call(task: str):
    model = pantheon_route(task)
    try:
        res = provider_registry.invoke(model, task)
        return res
    finally:
        ledger_record(model=model, task=task, result=locals().get("res"))
