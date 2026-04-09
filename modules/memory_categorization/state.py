"""
StateManager: Manage identity, today, workspace state

Provides:
- load_identity(): User identity info
- load_today(): Today's date and schedule
- load_workspace(): Active workspace state
- save_state(): Persist state changes
"""

import logging
from typing import Dict, Any, Optional
from datetime import datetime, timezone
from pathlib import Path
import json
import asyncio

logger = logging.getLogger(__name__)


class StateManager:
    """Manages session state (identity, today, workspace)"""

    def __init__(self, db_pool=None, state_dir: Optional[str] = None):
        """Initialize state manager

        Args:
            db_pool: Database connection pool
            state_dir: Directory for state files (optional)
        """
        self.db_pool = db_pool
        self.state_dir = Path(state_dir) if state_dir else Path.home() / '.mnemos' / 'state'
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._cache = {}

    async def load_identity(self) -> Dict[str, Any]:
        """Load user identity state

        Returns:
            Identity dict with name, id, metadata
        """
        logger.debug("Loading identity state")

        # Try cache first
        if 'identity' in self._cache:
            return self._cache['identity']

        identity = {
            'id': 'unknown',
            'name': 'Unknown User',
            'workspace': 'default',
            'version': 1,
        }

        # Try database
        if self.db_pool:
            try:
                async with self.db_pool.acquire() as conn:
                    row = await conn.fetchrow(
                        'SELECT * FROM state WHERE key = $1',
                        'identity'
                    )
                    if row:
                        identity = dict(row)
                        logger.debug(f"Loaded identity from database: {identity['name']}")
            except Exception as e:
                logger.debug(f"Could not load identity from database: {e}")

        # Try state file
        state_file = self.state_dir / 'identity.json'
        if state_file.exists():
            try:
                with open(state_file) as f:
                    file_identity = json.load(f)
                    identity.update(file_identity)
                    logger.debug(f"Loaded identity from file: {identity['name']}")
            except Exception as e:
                logger.debug(f"Could not load identity from file: {e}")

        self._cache['identity'] = identity
        return identity

    async def load_today(self) -> Dict[str, Any]:
        """Load today's state (date, schedule, events)

        Returns:
            Today dict with date, schedule, events
        """
        logger.debug("Loading today state")

        # Try cache first
        if 'today' in self._cache:
            return self._cache['today']

        today = {
            'date': datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
            'day_of_week': datetime.now(timezone.utc).replace(tzinfo=None).strftime('%A'),
            'schedule': [],
            'events': [],
            'version': 1,
        }

        # Try database
        if self.db_pool:
            try:
                async with self.db_pool.acquire() as conn:
                    row = await conn.fetchrow(
                        'SELECT * FROM state WHERE key = $1',
                        'today'
                    )
                    if row:
                        today = dict(row)
                        logger.debug("Loaded today from database")
            except Exception as e:
                logger.debug(f"Could not load today from database: {e}")

        # Try state file
        state_file = self.state_dir / 'today.json'
        if state_file.exists():
            try:
                with open(state_file) as f:
                    file_today = json.load(f)
                    today.update(file_today)
                    logger.debug("Loaded today from file")
            except Exception as e:
                logger.debug(f"Could not load today from file: {e}")

        self._cache['today'] = today
        return today

    async def load_workspace(self) -> Dict[str, Any]:
        """Load workspace state (active projects, settings)

        Returns:
            Workspace dict with projects, settings, metadata
        """
        logger.debug("Loading workspace state")

        # Try cache first
        if 'workspace' in self._cache:
            return self._cache['workspace']

        workspace = {
            'id': 'default',
            'name': 'Default Workspace',
            'active_project': None,
            'projects': [],
            'settings': {},
            'version': 1,
        }

        # Try database
        if self.db_pool:
            try:
                async with self.db_pool.acquire() as conn:
                    row = await conn.fetchrow(
                        'SELECT * FROM state WHERE key = $1',
                        'workspace'
                    )
                    if row:
                        workspace = dict(row)
                        logger.debug("Loaded workspace from database")
            except Exception as e:
                logger.debug(f"Could not load workspace from database: {e}")

        # Try state file
        state_file = self.state_dir / 'workspace.json'
        if state_file.exists():
            try:
                with open(state_file) as f:
                    file_workspace = json.load(f)
                    workspace.update(file_workspace)
                    logger.debug("Loaded workspace from file")
            except Exception as e:
                logger.debug(f"Could not load workspace from file: {e}")

        self._cache['workspace'] = workspace
        return workspace

    async def save_state(self, state: Dict[str, Any], key: str) -> None:
        """Save state to database and file

        Args:
            state: State dict to save
            key: State key (identity, today, workspace)
        """
        logger.debug(f"Saving state: {key}")

        # Add timestamp
        state['updated_at'] = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()

        # Save to cache
        self._cache[key] = state

        # Save to file
        state_file = self.state_dir / f'{key}.json'
        try:
            with open(state_file, 'w') as f:
                json.dump(state, f, indent=2)
            logger.debug(f"Saved {key} state to file")
        except Exception as e:
            logger.error(f"Error saving {key} to file: {e}")

        # Save to database
        if self.db_pool:
            try:
                async with self.db_pool.acquire() as conn:
                    # Upsert to state table
                    await conn.execute(
                        '''INSERT INTO state (key, value, updated_at)
                           VALUES ($1, $2, $3)
                           ON CONFLICT (key) DO UPDATE SET
                           value = $2, updated_at = $3''',
                        key,
                        json.dumps(state),
                        state['updated_at']
                    )
                logger.debug(f"Saved {key} state to database")
            except Exception as e:
                logger.error(f"Error saving {key} to database: {e}")

    async def clear_cache(self) -> None:
        """Clear in-memory state cache"""
        self._cache.clear()
        logger.debug("Cleared state cache")

    async def reset_state(self, key: Optional[str] = None) -> None:
        """Reset state to defaults

        Args:
            key: Specific key to reset, or None for all
        """
        if key:
            keys = [key]
        else:
            keys = ['identity', 'today', 'workspace']

        for k in keys:
            # Remove from cache
            self._cache.pop(k, None)

            # Remove state file
            state_file = self.state_dir / f'{k}.json'
            if state_file.exists():
                state_file.unlink()
                logger.debug(f"Reset state file: {k}")

            # Remove from database
            if self.db_pool:
                try:
                    async with self.db_pool.acquire() as conn:
                        await conn.execute(
                            'DELETE FROM state WHERE key = $1',
                            k
                        )
                    logger.debug(f"Reset database state: {k}")
                except Exception as e:
                    logger.error(f"Error resetting {k} in database: {e}")
