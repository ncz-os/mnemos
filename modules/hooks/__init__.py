"""
Hooks System: Event-driven extensibility for MNEMOS

Provides:
- HookRegistry: Central event dispatcher
- Built-in hooks: session-start, prompt-submit
- Configuration-driven enable/disable
- Error handling (log, don't crash)
"""

from .hook_registry import HookRegistry, HookEvent
from .session_start import SessionStartHook
from .prompt_submit import PromptSubmitHook

# Hook event constants
HOOK_SESSION_START = "session.start"
HOOK_PROMPT_SUBMIT = "prompt.submit"
HOOK_MEMORY_WRITE = "memory.write"
HOOK_MEMORY_READ = "memory.read"
HOOK_REHYDRATION_START = "rehydration.start"
HOOK_GRAEAE_CONSULT = "graeae.consult"

__all__ = [
    'HookRegistry',
    'HookEvent',
    'SessionStartHook',
    'PromptSubmitHook',
    'HOOK_SESSION_START',
    'HOOK_PROMPT_SUBMIT',
    'HOOK_MEMORY_WRITE',
    'HOOK_MEMORY_READ',
    'HOOK_REHYDRATION_START',
    'HOOK_GRAEAE_CONSULT',
]
