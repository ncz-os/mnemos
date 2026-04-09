"""
EntityManager: Track entities and relationships

Provides:
- create_entity(): Create entity (person, project, concept)
- link_entities(): Create relationship between entities
- query_entities(): Find entities by type
- get_entity_relations(): Get entity relationships
"""

import logging
from typing import List, Dict, Any, Optional, Set
from uuid import uuid4
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class Entity:
    """Represents an entity (person, project, concept)"""

    TYPES = ['person', 'project', 'concept', 'document', 'decision', 'event']

    def __init__(self, entity_type: str, name: str, metadata: Optional[Dict] = None):
        self.id = str(uuid4())
        self.entity_type = entity_type
        self.name = name
        self.metadata = metadata or {}
        self.created_at = datetime.now(timezone.utc).replace(tzinfo=None)
        self.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'entity_type': self.entity_type,
            'name': self.name,
            'metadata': self.metadata,
            'created_at': self.created_at.isoformat(),
            'updated_at': self.updated_at.isoformat(),
        }


class EntityRelationship:
    """Represents relationship between entities"""

    def __init__(self, entity1_id: str, entity2_id: str,
                relation_type: str, metadata: Optional[Dict] = None):
        self.id = str(uuid4())
        self.entity1_id = entity1_id
        self.entity2_id = entity2_id
        self.relation_type = relation_type  # 'works_on', 'related_to', 'depends_on', etc
        self.metadata = metadata or {}
        self.created_at = datetime.now(timezone.utc).replace(tzinfo=None)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'entity1_id': self.entity1_id,
            'entity2_id': self.entity2_id,
            'relation_type': self.relation_type,
            'metadata': self.metadata,
            'created_at': self.created_at.isoformat(),
        }


class EntityManager:
    """Manages entities and relationships"""

    def __init__(self, db_pool=None):
        """Initialize entity manager

        Args:
            db_pool: Database connection pool
        """
        self.db_pool = db_pool
        self._entity_cache: Dict[str, Entity] = {}

    async def create_entity(self, entity_type: str, name: str,
                          metadata: Optional[Dict] = None) -> str:
        """Create entity

        Args:
            entity_type: Type ('person', 'project', 'concept', etc)
            name: Entity name
            metadata: Optional metadata

        Returns:
            Entity ID
        """
        if entity_type not in Entity.TYPES:
            logger.warning(f"Unknown entity type: {entity_type}")

        entity = Entity(entity_type, name, metadata)
        logger.debug(f"Creating entity: {entity_type} - {name}")

        # Cache
        self._entity_cache[entity.id] = entity

        # Save to database
        if self.db_pool:
            try:
                async with self.db_pool.acquire() as conn:
                    await conn.execute(
                        '''INSERT INTO entities (id, type, name, metadata)
                           VALUES ($1, $2, $3, $4)''',
                        entity.id,
                        entity.entity_type,
                        entity.name,
                        entity.metadata
                    )
                logger.debug(f"Saved entity to database: {entity.id}")
            except Exception as e:
                logger.error(f"Error saving entity: {e}", exc_info=True)

        return entity.id

    async def get_entity(self, entity_id: str) -> Optional[Dict]:
        """Get entity by ID

        Args:
            entity_id: Entity ID

        Returns:
            Entity dict or None
        """
        # Check cache
        if entity_id in self._entity_cache:
            return self._entity_cache[entity_id].to_dict()

        # Query database
        if self.db_pool:
            try:
                async with self.db_pool.acquire() as conn:
                    row = await conn.fetchrow(
                        'SELECT * FROM entities WHERE id = $1',
                        entity_id
                    )
                    if row:
                        return dict(row)
            except Exception as e:
                logger.error(f"Error fetching entity: {e}", exc_info=True)

        return None

    async def query_entities(self, entity_type: Optional[str] = None,
                           name_filter: Optional[str] = None) -> List[Dict]:
        """Query entities

        Args:
            entity_type: Filter by type (optional)
            name_filter: Filter by name substring (optional)

        Returns:
            List of entity dicts
        """
        logger.debug(f"Querying entities: type={entity_type}, name={name_filter}")

        entities = []

        if self.db_pool:
            try:
                async with self.db_pool.acquire() as conn:
                    if entity_type and name_filter:
                        rows = await conn.fetch(
                            '''SELECT * FROM entities
                               WHERE type = $1 AND name ILIKE $2
                               ORDER BY created_at DESC''',
                            entity_type,
                            f'%{name_filter}%'
                        )
                    elif entity_type:
                        rows = await conn.fetch(
                            '''SELECT * FROM entities
                               WHERE type = $1
                               ORDER BY created_at DESC''',
                            entity_type
                        )
                    elif name_filter:
                        rows = await conn.fetch(
                            '''SELECT * FROM entities
                               WHERE name ILIKE $1
                               ORDER BY created_at DESC''',
                            f'%{name_filter}%'
                        )
                    else:
                        rows = await conn.fetch(
                            'SELECT * FROM entities ORDER BY created_at DESC'
                        )

                    entities = [dict(row) for row in rows]
                    logger.debug(f"Found {len(entities)} entities")
            except Exception as e:
                logger.error(f"Error querying entities: {e}", exc_info=True)

        return entities

    async def link_entities(self, entity1_id: str, entity2_id: str,
                          relation_type: str = 'related_to',
                          metadata: Optional[Dict] = None) -> str:
        """Create relationship between entities

        Args:
            entity1_id: First entity ID
            entity2_id: Second entity ID
            relation_type: Type of relationship
            metadata: Optional metadata

        Returns:
            Relationship ID
        """
        relationship = EntityRelationship(
            entity1_id, entity2_id, relation_type, metadata
        )
        logger.debug(f"Creating relationship: {entity1_id} {relation_type} {entity2_id}")

        # Save to database
        if self.db_pool:
            try:
                async with self.db_pool.acquire() as conn:
                    await conn.execute(
                        '''INSERT INTO entity_relationships
                           (id, entity1_id, entity2_id, relation_type, metadata)
                           VALUES ($1, $2, $3, $4, $5)''',
                        relationship.id,
                        relationship.entity1_id,
                        relationship.entity2_id,
                        relationship.relation_type,
                        relationship.metadata
                    )
                logger.debug(f"Saved relationship to database: {relationship.id}")
            except Exception as e:
                logger.error(f"Error saving relationship: {e}", exc_info=True)

        return relationship.id

    async def get_entity_relations(self, entity_id: str,
                                  relation_type: Optional[str] = None) -> List[Dict]:
        """Get relationships for entity

        Args:
            entity_id: Entity ID
            relation_type: Filter by relationship type (optional)

        Returns:
            List of relationship dicts
        """
        logger.debug(f"Getting relations for entity: {entity_id}")

        relations = []

        if self.db_pool:
            try:
                async with self.db_pool.acquire() as conn:
                    if relation_type:
                        rows = await conn.fetch(
                            '''SELECT * FROM entity_relationships
                               WHERE (entity1_id = $1 OR entity2_id = $1)
                               AND relation_type = $2
                               ORDER BY created_at DESC''',
                            entity_id,
                            relation_type
                        )
                    else:
                        rows = await conn.fetch(
                            '''SELECT * FROM entity_relationships
                               WHERE entity1_id = $1 OR entity2_id = $1
                               ORDER BY created_at DESC''',
                            entity_id
                        )

                    relations = [dict(row) for row in rows]
                    logger.debug(f"Found {len(relations)} relations")
            except Exception as e:
                logger.error(f"Error getting relations: {e}", exc_info=True)

        return relations

    async def get_related_entities(self, entity_id: str,
                                  max_depth: int = 1) -> Dict[str, Any]:
        """Get entity and all related entities

        Args:
            entity_id: Entity ID
            max_depth: Maximum relationship depth to traverse

        Returns:
            Dict with entity and related entities
        """
        visited: Set[str] = set()
        result = {
            'entity': None,
            'relations': [],
            'related_entities': [],
        }

        # Get the main entity
        entity = await self.get_entity(entity_id)
        if not entity:
            return result

        result['entity'] = entity
        visited.add(entity_id)

        # Get relations
        relations = await self.get_entity_relations(entity_id)
        result['relations'] = relations

        # Get related entities
        for relation in relations:
            other_id = (relation['entity2_id'] if relation['entity1_id'] == entity_id
                       else relation['entity1_id'])

            if other_id not in visited:
                other_entity = await self.get_entity(other_id)
                if other_entity:
                    result['related_entities'].append({
                        'entity': other_entity,
                        'relation_type': relation['relation_type'],
                    })
                visited.add(other_id)

        return result

    async def get_statistics(self) -> Dict[str, Any]:
        """Get entity statistics

        Returns:
            Dict with counts and types
        """
        stats = {
            'total_entities': 0,
            'by_type': {},
            'total_relationships': 0,
        }

        if self.db_pool:
            try:
                async with self.db_pool.acquire() as conn:
                    # Total entities
                    total = await conn.fetchval('SELECT COUNT(*) FROM entities')
                    stats['total_entities'] = total or 0

                    # By type
                    type_rows = await conn.fetch(
                        '''SELECT type, COUNT(*) as count FROM entities
                           GROUP BY type ORDER BY count DESC'''
                    )
                    stats['by_type'] = {row['type']: row['count'] for row in type_rows}

                    # Total relationships
                    rel_count = await conn.fetchval(
                        'SELECT COUNT(*) FROM entity_relationships'
                    )
                    stats['total_relationships'] = rel_count or 0

                logger.debug(f"Entity stats: {stats['total_entities']} entities")
            except Exception as e:
                logger.error(f"Error getting statistics: {e}", exc_info=True)

        return stats
