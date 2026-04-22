"""
EntityManager: Track entities and their relationships.

DB schema uses related_entities UUID[] array (no join table).

Provides:
- create_entity(): Create entity (person, project, concept, etc.)
- get_entity(): Fetch single entity
- link_entities(): Add entity ID to related_entities array
- query_entities(): Search entities by type or name
- get_related_entities(): Traverse entity relationships
- delete_entity(): Remove entity
"""

# Library API: This module provides a programmatic interface to the journal/state/entities
# subsystem for use in Python applications that embed MNEMOS directly.
# The REST API handlers (api/handlers/) use direct asyncpg queries for performance.

import logging
from typing import List, Dict, Any, Optional
from uuid import uuid4

logger = logging.getLogger(__name__)

from modules.memory_categorization.constants import ENTITY_TYPES


class EntityManager:
    """Manages entities using the entities table (related_entities UUID[] for links)."""

    def __init__(self, db_pool=None):
        self.db_pool = db_pool

    async def create_entity(self, entity_type: str, name: str,
                            description: Optional[str] = None,
                            metadata: Optional[Dict] = None) -> Optional[str]:
        """Create entity. Returns entity id or None on error."""
        if entity_type not in ENTITY_TYPES:
            logger.warning(f"Unknown entity type: {entity_type}")
        entity_id = str(uuid4())
        if not self.db_pool:
            return entity_id
        try:
            async with self.db_pool.acquire() as conn:
                await conn.execute(
                    '''INSERT INTO entities (id, entity_type, name, description, metadata)
                       VALUES ($1, $2, $3, $4, $5)
                       ON CONFLICT (entity_type, name) DO NOTHING''',
                    entity_id, entity_type, name, description, metadata or {}
                )
            logger.debug(f"Created entity: {entity_type}/{name}")
            return entity_id
        except Exception as e:
            logger.error(f"Error creating entity: {e}", exc_info=True)
            return None

    async def get_entity(self, entity_id: str) -> Optional[Dict]:
        """Fetch entity by id."""
        if not self.db_pool:
            return None
        try:
            async with self.db_pool.acquire() as conn:
                row = await conn.fetchrow('SELECT * FROM entities WHERE id = $1', entity_id)
                return dict(row) if row else None
        except Exception as e:
            logger.error(f"Error fetching entity: {e}", exc_info=True)
            return None

    async def get_by_name(self, entity_type: str, name: str) -> Optional[Dict]:
        """Fetch entity by type+name."""
        if not self.db_pool:
            return None
        try:
            async with self.db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    'SELECT * FROM entities WHERE entity_type = $1 AND name = $2',
                    entity_type, name
                )
                return dict(row) if row else None
        except Exception as e:
            logger.error(f"Error fetching entity by name: {e}", exc_info=True)
            return None

    async def link_entities(self, entity_id: str, related_id: str) -> bool:
        """Add related_id to entity's related_entities array (bidirectional)."""
        if not self.db_pool:
            return False
        try:
            async with self.db_pool.acquire() as conn:
                await conn.execute(
                    '''UPDATE entities
                       SET related_entities = array_append(
                           COALESCE(related_entities, ARRAY[]::uuid[]),
                           $2::uuid
                       ),
                       updated = NOW()
                       WHERE id = $1 AND NOT ($2::uuid = ANY(COALESCE(related_entities, ARRAY[]::uuid[])))''',
                    entity_id, related_id
                )
                # Also link in reverse
                await conn.execute(
                    '''UPDATE entities
                       SET related_entities = array_append(
                           COALESCE(related_entities, ARRAY[]::uuid[]),
                           $2::uuid
                       ),
                       updated = NOW()
                       WHERE id = $1 AND NOT ($2::uuid = ANY(COALESCE(related_entities, ARRAY[]::uuid[])))''',
                    related_id, entity_id
                )
            logger.debug(f"Linked entities: {entity_id} <-> {related_id}")
            return True
        except Exception as e:
            logger.error(f"Error linking entities: {e}", exc_info=True)
            return False

    async def query_entities(self, entity_type: Optional[str] = None,
                             name_search: Optional[str] = None,
                             limit: int = 50) -> List[Dict]:
        """Search entities."""
        if not self.db_pool:
            return []
        try:
            async with self.db_pool.acquire() as conn:
                if entity_type and name_search:
                    rows = await conn.fetch(
                        '''SELECT * FROM entities WHERE entity_type = $1 AND name ILIKE $2
                           ORDER BY name LIMIT $3''',
                        entity_type, f'%{name_search}%', limit
                    )
                elif entity_type:
                    rows = await conn.fetch(
                        'SELECT * FROM entities WHERE entity_type = $1 ORDER BY name LIMIT $2',
                        entity_type, limit
                    )
                elif name_search:
                    rows = await conn.fetch(
                        'SELECT * FROM entities WHERE name ILIKE $1 ORDER BY name LIMIT $2',
                        f'%{name_search}%', limit
                    )
                else:
                    rows = await conn.fetch(
                        'SELECT * FROM entities ORDER BY entity_type, name LIMIT $1', limit
                    )
                return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"Error querying entities: {e}", exc_info=True)
            return []

    async def get_related_entities(self, entity_id: str) -> List[Dict]:
        """Get all entities linked to this one via related_entities array."""
        entity = await self.get_entity(entity_id)
        if not entity or not entity.get('related_entities'):
            return []
        related_ids = entity['related_entities']
        if not related_ids or not self.db_pool:
            return []
        try:
            async with self.db_pool.acquire() as conn:
                rows = await conn.fetch(
                    'SELECT * FROM entities WHERE id = ANY($1::uuid[])',
                    related_ids
                )
                return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"Error fetching related entities: {e}", exc_info=True)
            return []

    async def update_entity(self, entity_id: str,
                            description: Optional[str] = None,
                            metadata: Optional[Dict] = None) -> bool:
        """Update entity description/metadata."""
        if not self.db_pool:
            return False
        try:
            async with self.db_pool.acquire() as conn:
                if description is not None and metadata is not None:
                    await conn.execute(
                        'UPDATE entities SET description=$1, metadata=$2, updated=NOW() WHERE id=$3',
                        description, metadata, entity_id
                    )
                elif description is not None:
                    await conn.execute(
                        'UPDATE entities SET description=$1, updated=NOW() WHERE id=$2',
                        description, entity_id
                    )
                elif metadata is not None:
                    await conn.execute(
                        'UPDATE entities SET metadata=$1, updated=NOW() WHERE id=$2',
                        metadata, entity_id
                    )
            return True
        except Exception as e:
            logger.error(f"Error updating entity: {e}", exc_info=True)
            return False

    async def delete_entity(self, entity_id: str) -> bool:
        """Delete entity and remove from other entities' related_entities arrays."""
        if not self.db_pool:
            return False
        try:
            async with self.db_pool.acquire() as conn:
                # Remove from other entities' arrays first
                await conn.execute(
                    '''UPDATE entities
                       SET related_entities = array_remove(related_entities, $1::uuid)
                       WHERE $1::uuid = ANY(COALESCE(related_entities, ARRAY[]::uuid[]))''',
                    entity_id
                )
                result = await conn.execute('DELETE FROM entities WHERE id = $1', entity_id)
                return result != 'DELETE 0'
        except Exception as e:
            logger.error(f"Error deleting entity: {e}", exc_info=True)
            return False

    async def get_statistics(self) -> Dict[str, Any]:
        stats = {'total_entities': 0, 'by_type': {}}
        if not self.db_pool:
            return stats
        try:
            async with self.db_pool.acquire() as conn:
                stats['total_entities'] = await conn.fetchval('SELECT COUNT(*) FROM entities') or 0
                type_rows = await conn.fetch(
                    'SELECT entity_type, COUNT(*) as count FROM entities GROUP BY entity_type ORDER BY count DESC'
                )
                stats['by_type'] = {row['entity_type']: row['count'] for row in type_rows}
        except Exception as e:
            logger.error(f"Error getting entity stats: {e}", exc_info=True)
        return stats
