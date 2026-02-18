"""
JournalManager: Date-partitioned journal entry management

Provides:
- append(): Add journal entry
- get_recent(): Get recent entries
- query(): Search journal entries
- get_by_date(): Get entries for specific date
"""

import logging
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta
from uuid import uuid4

logger = logging.getLogger(__name__)


class JournalEntry:
    """Represents a journal entry"""

    def __init__(self, topic: str, content: str, metadata: Optional[Dict] = None):
        self.id = str(uuid4())
        self.topic = topic
        self.content = content
        self.metadata = metadata or {}
        self.created_at = datetime.utcnow()
        self.updated_at = datetime.utcnow()

    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'topic': self.topic,
            'content': self.content,
            'metadata': self.metadata,
            'created_at': self.created_at.isoformat(),
            'updated_at': self.updated_at.isoformat(),
            'date': self.created_at.date().isoformat(),
        }


class JournalManager:
    """Manages journal entries with date partitioning"""

    def __init__(self, db_pool=None):
        """Initialize journal manager

        Args:
            db_pool: Database connection pool
        """
        self.db_pool = db_pool
        self._cache = {}  # In-memory cache for recent entries

    async def append(self, topic: str, content: str,
                    metadata: Optional[Dict] = None) -> str:
        """Add journal entry

        Args:
            topic: Entry topic/category
            content: Entry content
            metadata: Optional metadata

        Returns:
            Entry ID
        """
        entry = JournalEntry(topic, content, metadata)
        logger.debug(f"Adding journal entry: {topic}")

        # Save to database
        if self.db_pool:
            try:
                async with self.db_pool.acquire() as conn:
                    await conn.execute(
                        '''INSERT INTO journal (id, topic, content, metadata, date)
                           VALUES ($1, $2, $3, $4, $5)''',
                        entry.id,
                        entry.topic,
                        entry.content,
                        entry.metadata,
                        entry.created_at.date()
                    )
                logger.debug(f"Saved journal entry to database: {entry.id}")
            except Exception as e:
                logger.error(f"Error saving journal entry: {e}", exc_info=True)

        return entry.id

    async def get_recent(self, count: int = 10, topic: Optional[str] = None) -> List[Dict]:
        """Get recent journal entries

        Args:
            count: Number of entries to retrieve
            topic: Filter by topic (optional)

        Returns:
            List of entry dicts, most recent first
        """
        logger.debug(f"Fetching {count} recent journal entries")

        entries = []

        if self.db_pool:
            try:
                async with self.db_pool.acquire() as conn:
                    if topic:
                        rows = await conn.fetch(
                            '''SELECT * FROM journal
                               WHERE topic = $1
                               ORDER BY created_at DESC
                               LIMIT $2''',
                            topic, count
                        )
                    else:
                        rows = await conn.fetch(
                            '''SELECT * FROM journal
                               ORDER BY created_at DESC
                               LIMIT $1''',
                            count
                        )
                    entries = [dict(row) for row in rows]
                    logger.debug(f"Retrieved {len(entries)} entries from database")
            except Exception as e:
                logger.error(f"Error fetching recent entries: {e}", exc_info=True)

        return entries

    async def query(self, search: str, limit: int = 20) -> List[Dict]:
        """Search journal entries

        Args:
            search: Search query
            limit: Maximum results

        Returns:
            List of matching entry dicts
        """
        logger.debug(f"Searching journal: {search}")

        entries = []

        if self.db_pool:
            try:
                async with self.db_pool.acquire() as conn:
                    rows = await conn.fetch(
                        '''SELECT * FROM journal
                           WHERE content ILIKE $1 OR topic ILIKE $1
                           ORDER BY created_at DESC
                           LIMIT $2''',
                        f'%{search}%',
                        limit
                    )
                    entries = [dict(row) for row in rows]
                    logger.debug(f"Found {len(entries)} matching entries")
            except Exception as e:
                logger.error(f"Error searching journal: {e}", exc_info=True)

        return entries

    async def get_by_date(self, date_str: str) -> List[Dict]:
        """Get journal entries for specific date

        Args:
            date_str: Date in YYYY-MM-DD format

        Returns:
            List of entries for that date
        """
        logger.debug(f"Fetching entries for date: {date_str}")

        entries = []

        if self.db_pool:
            try:
                async with self.db_pool.acquire() as conn:
                    rows = await conn.fetch(
                        '''SELECT * FROM journal
                           WHERE date = $1
                           ORDER BY created_at DESC''',
                        date_str
                    )
                    entries = [dict(row) for row in rows]
                    logger.debug(f"Retrieved {len(entries)} entries for {date_str}")
            except Exception as e:
                logger.error(f"Error fetching entries by date: {e}", exc_info=True)

        return entries

    async def get_date_range(self, start_date: str, end_date: str) -> List[Dict]:
        """Get journal entries for date range

        Args:
            start_date: Start date (YYYY-MM-DD)
            end_date: End date (YYYY-MM-DD)

        Returns:
            List of entries in range
        """
        logger.debug(f"Fetching entries from {start_date} to {end_date}")

        entries = []

        if self.db_pool:
            try:
                async with self.db_pool.acquire() as conn:
                    rows = await conn.fetch(
                        '''SELECT * FROM journal
                           WHERE date BETWEEN $1 AND $2
                           ORDER BY created_at DESC''',
                        start_date,
                        end_date
                    )
                    entries = [dict(row) for row in rows]
                    logger.debug(f"Retrieved {len(entries)} entries")
            except Exception as e:
                logger.error(f"Error fetching date range: {e}", exc_info=True)

        return entries

    async def get_statistics(self) -> Dict[str, Any]:
        """Get journal statistics

        Returns:
            Dict with entry counts, topics, etc.
        """
        stats = {
            'total_entries': 0,
            'topics': {},
            'entries_today': 0,
            'entries_this_week': 0,
        }

        if self.db_pool:
            try:
                async with self.db_pool.acquire() as conn:
                    # Total entries
                    total = await conn.fetchval('SELECT COUNT(*) FROM journal')
                    stats['total_entries'] = total or 0

                    # Topics
                    topic_rows = await conn.fetch(
                        '''SELECT topic, COUNT(*) as count FROM journal
                           GROUP BY topic ORDER BY count DESC'''
                    )
                    stats['topics'] = {row['topic']: row['count'] for row in topic_rows}

                    # Today
                    today = datetime.utcnow().date()
                    today_count = await conn.fetchval(
                        'SELECT COUNT(*) FROM journal WHERE date = $1',
                        today
                    )
                    stats['entries_today'] = today_count or 0

                    # This week
                    week_ago = datetime.utcnow().date() - timedelta(days=7)
                    week_count = await conn.fetchval(
                        'SELECT COUNT(*) FROM journal WHERE date >= $1',
                        week_ago
                    )
                    stats['entries_this_week'] = week_count or 0

                logger.debug(f"Journal stats: {stats['total_entries']} total")
            except Exception as e:
                logger.error(f"Error getting statistics: {e}", exc_info=True)

        return stats
