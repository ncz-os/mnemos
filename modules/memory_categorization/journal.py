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
from datetime import datetime, timedelta, timezone
from uuid import uuid4

logger = logging.getLogger(__name__)


class JournalEntry:
    """Represents a journal entry"""

    def __init__(self, topic: str, content: str, metadata: Optional[Dict] = None):
        self.id = str(uuid4())
        self.topic = topic
        self.content = content
        self.metadata = metadata or {}
        self.created_at = datetime.now(timezone.utc).replace(tzinfo=None)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'topic': self.topic,
            'content': self.content,
            'metadata': self.metadata,
            'created': self.created_at.isoformat(),
            'entry_date': self.created_at.date().isoformat(),
        }


class JournalManager:
    """Manages journal entries with date partitioning"""

    def __init__(self, db_pool=None):
        self.db_pool = db_pool

    async def append(self, topic: str, content: str,
                     metadata: Optional[Dict] = None) -> str:
        entry = JournalEntry(topic, content, metadata)
        logger.debug(f"Adding journal entry: {topic}")

        if self.db_pool:
            try:
                async with self.db_pool.acquire() as conn:
                    await conn.execute(
                        '''INSERT INTO journal (id, entry_date, topic, content, metadata)
                           VALUES ($1, $2, $3, $4, $5)''',
                        entry.id,
                        entry.created_at.date(),
                        entry.topic,
                        entry.content,
                        entry.metadata,
                    )
                logger.debug(f"Saved journal entry: {entry.id}")
            except Exception as e:
                logger.error(f"Error saving journal entry: {e}", exc_info=True)

        return entry.id

    async def get_recent(self, count: int = 10, topic: Optional[str] = None) -> List[Dict]:
        entries = []
        if self.db_pool:
            try:
                async with self.db_pool.acquire() as conn:
                    if topic:
                        rows = await conn.fetch(
                            'SELECT * FROM journal WHERE topic = $1 ORDER BY created DESC LIMIT $2',
                            topic, count
                        )
                    else:
                        rows = await conn.fetch(
                            'SELECT * FROM journal ORDER BY created DESC LIMIT $1',
                            count
                        )
                    entries = [dict(row) for row in rows]
            except Exception as e:
                logger.error(f"Error fetching recent entries: {e}", exc_info=True)
        return entries

    async def query(self, search: str, limit: int = 20) -> List[Dict]:
        entries = []
        if self.db_pool:
            try:
                async with self.db_pool.acquire() as conn:
                    rows = await conn.fetch(
                        '''SELECT * FROM journal
                           WHERE content ILIKE $1 OR topic ILIKE $1
                           ORDER BY created DESC LIMIT $2''',
                        f'%{search}%', limit
                    )
                    entries = [dict(row) for row in rows]
            except Exception as e:
                logger.error(f"Error searching journal: {e}", exc_info=True)
        return entries

    async def get_by_date(self, date_str: str) -> List[Dict]:
        entries = []
        if self.db_pool:
            try:
                async with self.db_pool.acquire() as conn:
                    rows = await conn.fetch(
                        'SELECT * FROM journal WHERE entry_date = $1 ORDER BY created DESC',
                        date_str
                    )
                    entries = [dict(row) for row in rows]
            except Exception as e:
                logger.error(f"Error fetching entries by date: {e}", exc_info=True)
        return entries

    async def get_date_range(self, start_date: str, end_date: str) -> List[Dict]:
        entries = []
        if self.db_pool:
            try:
                async with self.db_pool.acquire() as conn:
                    rows = await conn.fetch(
                        '''SELECT * FROM journal
                           WHERE entry_date BETWEEN $1 AND $2
                           ORDER BY created DESC''',
                        start_date, end_date
                    )
                    entries = [dict(row) for row in rows]
            except Exception as e:
                logger.error(f"Error fetching date range: {e}", exc_info=True)
        return entries

    async def get_statistics(self) -> Dict[str, Any]:
        stats = {
            'total_entries': 0,
            'topics': {},
            'entries_today': 0,
            'entries_this_week': 0,
        }
        if self.db_pool:
            try:
                async with self.db_pool.acquire() as conn:
                    stats['total_entries'] = await conn.fetchval('SELECT COUNT(*) FROM journal') or 0
                    topic_rows = await conn.fetch(
                        'SELECT topic, COUNT(*) as count FROM journal GROUP BY topic ORDER BY count DESC'
                    )
                    stats['topics'] = {row['topic']: row['count'] for row in topic_rows}
                    today = datetime.now(timezone.utc).date()
                    stats['entries_today'] = await conn.fetchval(
                        'SELECT COUNT(*) FROM journal WHERE entry_date = $1', today
                    ) or 0
                    week_ago = datetime.now(timezone.utc).date() - timedelta(days=7)
                    stats['entries_this_week'] = await conn.fetchval(
                        'SELECT COUNT(*) FROM journal WHERE entry_date >= $1', week_ago
                    ) or 0
            except Exception as e:
                logger.error(f"Error getting statistics: {e}", exc_info=True)
        return stats
