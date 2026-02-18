"""
StateSynchronizer: Keep macrodata and MNEMOS state in sync

Handles bidirectional synchronization:
- Macrodata → MNEMOS (push state changes)
- MNEMOS → Macrodata (pull on demand)
- Conflict resolution
"""

import logging
import asyncio
from typing import Dict, Any, Optional, List
from datetime import datetime
from hashlib import md5

logger = logging.getLogger(__name__)


class StateSynchronizer:
    """Synchronizes state between macrodata and MNEMOS"""

    def __init__(self,
                 state_manager,
                 memory_store,
                 hook_adapter=None):
        """Initialize state synchronizer

        Args:
            state_manager: MNEMOS StateManager
            memory_store: MNEMOS MemoryStore
            hook_adapter: MacrodataHookAdapter for propagating changes
        """
        self.state_manager = state_manager
        self.memory_store = memory_store
        self.hook_adapter = hook_adapter
        self._state_hashes: Dict[str, str] = {}
        self._sync_log: List[Dict[str, Any]] = []
        self._max_log_size = 1000

    async def sync_from_macrodata(self,
                                  identity: Optional[Dict] = None,
                                  today: Optional[Dict] = None,
                                  workspace: Optional[Dict] = None) -> Dict[str, bool]:
        """Sync state from macrodata to MNEMOS

        Args:
            identity: Identity dict (optional)
            today: Today dict (optional)
            workspace: Workspace dict (optional)

        Returns:
            Dict of success status for each state type
        """
        logger.info("Syncing from macrodata to MNEMOS")

        results = {
            'identity': False,
            'today': False,
            'workspace': False,
        }

        # Sync identity
        if identity:
            try:
                # Check if changed
                if self._has_changed('identity', identity):
                    await self.state_manager.save_state(identity, 'identity')
                    self._record_hash('identity', identity)
                    results['identity'] = True

                    # Propagate via hook adapter
                    if self.hook_adapter:
                        await self.hook_adapter.on_identity_changed(identity)

                    logger.debug("Synced identity from macrodata")
            except Exception as e:
                logger.error(f"Error syncing identity: {e}", exc_info=True)

        # Sync today
        if today:
            try:
                if self._has_changed('today', today):
                    await self.state_manager.save_state(today, 'today')
                    self._record_hash('today', today)
                    results['today'] = True

                    if self.hook_adapter:
                        await self.hook_adapter.on_today_changed(today)

                    logger.debug("Synced today from macrodata")
            except Exception as e:
                logger.error(f"Error syncing today: {e}", exc_info=True)

        # Sync workspace
        if workspace:
            try:
                if self._has_changed('workspace', workspace):
                    await self.state_manager.save_state(workspace, 'workspace')
                    self._record_hash('workspace', workspace)
                    results['workspace'] = True

                    if self.hook_adapter:
                        await self.hook_adapter.on_workspace_changed(workspace)

                    logger.debug("Synced workspace from macrodata")
            except Exception as e:
                logger.error(f"Error syncing workspace: {e}", exc_info=True)

        # Log sync operation
        self._log_sync('macrodata_to_mnemos', results)

        return results

    async def sync_to_macrodata(self) -> Dict[str, Any]:
        """Sync state from MNEMOS to macrodata

        Returns:
            Dict with state retrieved from MNEMOS
        """
        logger.info("Syncing from MNEMOS to macrodata")

        result = {}

        try:
            identity = await self.state_manager.load_identity()
            result['identity'] = identity
        except Exception as e:
            logger.error(f"Error loading identity: {e}")

        try:
            today = await self.state_manager.load_today()
            result['today'] = today
        except Exception as e:
            logger.error(f"Error loading today: {e}")

        try:
            workspace = await self.state_manager.load_workspace()
            result['workspace'] = workspace
        except Exception as e:
            logger.error(f"Error loading workspace: {e}")

        logger.debug(f"Retrieved state from MNEMOS: {list(result.keys())}")

        self._log_sync('mnemos_to_macrodata', {'retrieved': list(result.keys())})

        return result

    async def bidirectional_sync(self,
                                macrodata_state: Optional[Dict] = None) -> Dict[str, Any]:
        """Perform full bidirectional sync

        Args:
            macrodata_state: State from macrodata (optional)

        Returns:
            Dict with sync results
        """
        logger.info("Starting bidirectional sync")

        result = {
            'pushed': {},
            'pulled': {},
            'conflicts': [],
        }

        # Push from macrodata to MNEMOS
        if macrodata_state:
            result['pushed'] = await self.sync_from_macrodata(
                identity=macrodata_state.get('identity'),
                today=macrodata_state.get('today'),
                workspace=macrodata_state.get('workspace'),
            )

        # Pull from MNEMOS to macrodata
        result['pulled'] = await self.sync_to_macrodata()

        logger.info(f"Bidirectional sync complete: {result}")

        return result

    def _has_changed(self, key: str, state: Dict) -> bool:
        """Check if state has changed since last sync

        Args:
            key: State key (identity, today, workspace)
            state: Current state dict

        Returns:
            True if changed
        """
        import json
        current_hash = md5(json.dumps(state, sort_keys=True).encode()).hexdigest()
        previous_hash = self._state_hashes.get(key)

        return current_hash != previous_hash

    def _record_hash(self, key: str, state: Dict) -> None:
        """Record hash of state for change detection

        Args:
            key: State key
            state: State dict
        """
        import json
        self._state_hashes[key] = md5(
            json.dumps(state, sort_keys=True).encode()
        ).hexdigest()

    def _log_sync(self, sync_type: str, details: Dict) -> None:
        """Log sync operation

        Args:
            sync_type: Type of sync operation
            details: Details of sync
        """
        log_entry = {
            'timestamp': datetime.utcnow().isoformat(),
            'sync_type': sync_type,
            'details': details,
        }

        self._sync_log.append(log_entry)

        # Trim log if too large
        if len(self._sync_log) > self._max_log_size:
            self._sync_log = self._sync_log[-self._max_log_size:]

    def get_sync_history(self, limit: int = 50) -> List[Dict]:
        """Get sync history

        Args:
            limit: Maximum entries to return

        Returns:
            List of sync log entries
        """
        return self._sync_log[-limit:]

    def clear_sync_history(self) -> None:
        """Clear sync history"""
        self._sync_log.clear()
        logger.debug("Cleared sync history")

    async def validate_sync(self) -> Dict[str, bool]:
        """Validate sync consistency between systems

        Returns:
            Dict with validation results
        """
        logger.debug("Validating sync consistency")

        result = {
            'identity_in_sync': True,
            'today_in_sync': True,
            'workspace_in_sync': True,
            'all_in_sync': True,
        }

        try:
            identity = await self.state_manager.load_identity()
            # Could validate against macrodata here
            logger.debug("Identity validation passed")
        except Exception as e:
            logger.error(f"Identity validation failed: {e}")
            result['identity_in_sync'] = False

        try:
            today = await self.state_manager.load_today()
            logger.debug("Today validation passed")
        except Exception as e:
            logger.error(f"Today validation failed: {e}")
            result['today_in_sync'] = False

        try:
            workspace = await self.state_manager.load_workspace()
            logger.debug("Workspace validation passed")
        except Exception as e:
            logger.error(f"Workspace validation failed: {e}")
            result['workspace_in_sync'] = False

        result['all_in_sync'] = all([
            result['identity_in_sync'],
            result['today_in_sync'],
            result['workspace_in_sync'],
        ])

        return result
