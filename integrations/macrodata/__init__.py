"""
Macrodata Integration: Sync state and auto-distill into MNEMOS

Provides:
- HookAdapter: Listen for macrodata state changes
- StateSync: Sync identity, today, workspace
- Auto-compression of state into memory
"""

from .hook_adapter import MacrodataHookAdapter
from .state_sync import StateSynchronizer

__all__ = [
    'MacrodataHookAdapter',
    'StateSynchronizer',
]
