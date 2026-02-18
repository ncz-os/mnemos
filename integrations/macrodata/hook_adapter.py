"""
MacrodataHookAdapter: Adapt macrodata hooks to MNEMOS

Listens for macrodata state changes and:
1. Compresses state into quality-rated memory blocks
2. Auto-saves to MNEMOS
3. Triggers rehydration hooks
4. Maintains audit trail
"""

import logging
import asyncio
from typing import Dict, Any, Optional, Callable
from datetime import datetime

logger = logging.getLogger(__name__)


class MacrodataHookAdapter:
    """Adapter between macrodata hooks and MNEMOS"""

    def __init__(self,
                 memory_store,
                 state_manager,
                 compression_manager,
                 quality_analyzer,
                 hook_registry=None):
        """Initialize macrodata hook adapter

        Args:
            memory_store: MemoryStore instance
            state_manager: StateManager instance
            compression_manager: CompressionManager instance
            quality_analyzer: QualityAnalyzer instance
            hook_registry: HookRegistry for triggering MNEMOS hooks
        """
        self.memory_store = memory_store
        self.state_manager = state_manager
        self.compression_manager = compression_manager
        self.quality_analyzer = quality_analyzer
        self.hook_registry = hook_registry
        self._callbacks: Dict[str, list] = {
            'identity_changed': [],
            'today_changed': [],
            'workspace_changed': [],
            'state_synced': [],
        }

    def register_callback(self, event: str, callback: Callable) -> None:
        """Register callback for macrodata event

        Args:
            event: Event name (identity_changed, today_changed, etc)
            callback: Async callback function
        """
        if event not in self._callbacks:
            self._callbacks[event] = []

        self._callbacks[event].append(callback)
        logger.debug(f"Registered callback for {event}")

    async def on_identity_changed(self, identity: Dict[str, Any]) -> None:
        """Handle identity state change

        Args:
            identity: New identity dict
        """
        logger.info("Macrodata identity changed")

        # Distill into memory block
        content = self._distill_identity(identity)

        try:
            # Save to MNEMOS with compression
            memory_id = await self.memory_store.save_memory({
                'content': content,
                'category': 'identity',
                'task_type': 'reasoning',
                'metadata': {
                    'source': 'macrodata',
                    'event': 'identity_changed',
                    'timestamp': datetime.utcnow().isoformat(),
                },
            })

            logger.debug(f"Saved identity memory: {memory_id}")

            # Save to state manager
            await self.state_manager.save_state(identity, 'identity')

            # Trigger callbacks
            for callback in self._callbacks['identity_changed']:
                try:
                    await callback(identity)
                except Exception as e:
                    logger.error(f"Callback error: {e}")

        except Exception as e:
            logger.error(f"Error processing identity change: {e}", exc_info=True)

    async def on_today_changed(self, today: Dict[str, Any]) -> None:
        """Handle today state change

        Args:
            today: New today dict
        """
        logger.info("Macrodata today changed")

        # Distill into memory block
        content = self._distill_today(today)

        try:
            # Save to MNEMOS
            memory_id = await self.memory_store.save_memory({
                'content': content,
                'category': 'facts',
                'task_type': 'reasoning',
                'metadata': {
                    'source': 'macrodata',
                    'event': 'today_changed',
                    'timestamp': datetime.utcnow().isoformat(),
                },
            })

            logger.debug(f"Saved today memory: {memory_id}")

            # Save to state manager
            await self.state_manager.save_state(today, 'today')

            # Trigger callbacks
            for callback in self._callbacks['today_changed']:
                try:
                    await callback(today)
                except Exception as e:
                    logger.error(f"Callback error: {e}")

        except Exception as e:
            logger.error(f"Error processing today change: {e}", exc_info=True)

    async def on_workspace_changed(self, workspace: Dict[str, Any]) -> None:
        """Handle workspace state change

        Args:
            workspace: New workspace dict
        """
        logger.info("Macrodata workspace changed")

        # Distill into memory block
        content = self._distill_workspace(workspace)

        try:
            # Save to MNEMOS
            memory_id = await self.memory_store.save_memory({
                'content': content,
                'category': 'preferences',
                'task_type': 'reasoning',
                'metadata': {
                    'source': 'macrodata',
                    'event': 'workspace_changed',
                    'timestamp': datetime.utcnow().isoformat(),
                },
            })

            logger.debug(f"Saved workspace memory: {memory_id}")

            # Save to state manager
            await self.state_manager.save_state(workspace, 'workspace')

            # Trigger callbacks
            for callback in self._callbacks['workspace_changed']:
                try:
                    await callback(workspace)
                except Exception as e:
                    logger.error(f"Callback error: {e}")

        except Exception as e:
            logger.error(f"Error processing workspace change: {e}", exc_info=True)

    async def sync_all(self, identity: Dict, today: Dict, workspace: Dict) -> None:
        """Sync all state at once

        Args:
            identity: Identity dict
            today: Today dict
            workspace: Workspace dict
        """
        logger.info("Syncing all macrodata state")

        # Run in parallel
        await asyncio.gather(
            self.on_identity_changed(identity),
            self.on_today_changed(today),
            self.on_workspace_changed(workspace),
            return_exceptions=True,
        )

        # Trigger rehydration hook if available
        if self.hook_registry:
            try:
                await self.hook_registry.trigger(
                    'rehydration.start',
                    {'source': 'macrodata_sync', 'timestamp': datetime.utcnow().isoformat()}
                )
            except Exception as e:
                logger.error(f"Error triggering rehydration hook: {e}")

        # Trigger callbacks
        for callback in self._callbacks['state_synced']:
            try:
                await callback({'identity': identity, 'today': today, 'workspace': workspace})
            except Exception as e:
                logger.error(f"Callback error: {e}")

    def _distill_identity(self, identity: Dict[str, Any]) -> str:
        """Distill identity into memory block

        Args:
            identity: Identity dict

        Returns:
            Distilled text
        """
        parts = []

        if 'name' in identity:
            parts.append(f"User: {identity['name']}")

        if 'id' in identity:
            parts.append(f"ID: {identity['id']}")

        if 'workspace' in identity:
            parts.append(f"Workspace: {identity['workspace']}")

        if 'metadata' in identity:
            for key, value in identity.get('metadata', {}).items():
                parts.append(f"{key}: {value}")

        return "\n".join(parts) if parts else "Identity state updated"

    def _distill_today(self, today: Dict[str, Any]) -> str:
        """Distill today into memory block

        Args:
            today: Today dict

        Returns:
            Distilled text
        """
        parts = []

        if 'date' in today:
            parts.append(f"Date: {today['date']}")

        if 'day_of_week' in today:
            parts.append(f"Day: {today['day_of_week']}")

        if 'schedule' in today and today['schedule']:
            parts.append(f"Schedule: {len(today['schedule'])} items")

        if 'events' in today and today['events']:
            parts.append(f"Events: {len(today['events'])} items")

        return "\n".join(parts) if parts else "Today state updated"

    def _distill_workspace(self, workspace: Dict[str, Any]) -> str:
        """Distill workspace into memory block

        Args:
            workspace: Workspace dict

        Returns:
            Distilled text
        """
        parts = []

        if 'name' in workspace:
            parts.append(f"Workspace: {workspace['name']}")

        if 'active_project' in workspace and workspace['active_project']:
            parts.append(f"Active Project: {workspace['active_project']}")

        if 'projects' in workspace and workspace['projects']:
            parts.append(f"Projects: {len(workspace['projects'])} total")

        if 'settings' in workspace:
            for key, value in workspace.get('settings', {}).items():
                parts.append(f"Setting: {key} = {value}")

        return "\n".join(parts) if parts else "Workspace state updated"
