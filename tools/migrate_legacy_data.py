#!/usr/bin/env python3
"""Migrate legacy MNEMOS JSON to new PostgreSQL schema"""
import json
import os
from pathlib import Path
import logging
import asyncio
import asyncpg

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

BACKUP_DIR = Path(os.getenv('MNEMOS_BACKUP_DIR', '/var/backups/mnemos'))

async def main():
    try:
        conn = await asyncpg.connect(
            user='mnemos_user',
            password=os.getenv('PG_PASSWORD', ''),
            database='mnemos',
            host='localhost'
        )
        
        # Migrate facts.json -> memories table
        facts_file = BACKUP_DIR / 'facts.json'
        if facts_file.exists():
            with open(facts_file) as f:
                facts = json.load(f)
            logger.info(f"Migrating {len(facts)} facts from facts.json...")
            for fact in facts:
                await conn.execute(
                    '''INSERT INTO memories (id, content, category, created, metadata)
                       VALUES ($1, $2, $3, NOW(), $4)
                       ON CONFLICT (id) DO NOTHING''',
                    str(fact.get('id', f'fact_{hash(fact.get("content", ""))}')),
                    fact.get('content', ''),
                    'facts',
                    json.dumps({'source': 'facts.json', **fact})
                )
            logger.info(f"✓ Inserted {len(facts)} facts")
        
        # Migrate projects.json -> memories as projects category
        projects_file = BACKUP_DIR / 'projects.json'
        if projects_file.exists():
            with open(projects_file) as f:
                projects = json.load(f)
            logger.info(f"Migrating {len(projects)} projects from projects.json...")
            for proj in projects:
                await conn.execute(
                    '''INSERT INTO memories (id, content, category, created, metadata)
                       VALUES ($1, $2, $3, NOW(), $4)
                       ON CONFLICT (id) DO NOTHING''',
                    str(proj.get('id', f'proj_{hash(proj.get("name", ""))}')),
                    json.dumps({'name': proj.get('name'), 'type': proj.get('type')}),
                    'projects',
                    json.dumps({'source': 'projects.json', **proj})
                )
            logger.info(f"✓ Inserted {len(projects)} projects")
        
        # Migrate preferences.json
        prefs_file = BACKUP_DIR / 'preferences.json'
        if prefs_file.exists():
            with open(prefs_file) as f:
                prefs = json.load(f)
            logger.info(f"Migrating preferences...")
            for pref in prefs:
                await conn.execute(
                    '''INSERT INTO memories (id, content, category, created, metadata)
                       VALUES ($1, $2, $3, NOW(), $4)
                       ON CONFLICT (id) DO NOTHING''',
                    str(pref.get('id', f'pref_{hash(json.dumps(pref))}')),
                    json.dumps(pref),
                    'preferences',
                    json.dumps({'source': 'preferences.json', **pref})
                )
            logger.info(f"✓ Preferences migrated")
        
        # Verify
        count = await conn.fetchval('SELECT COUNT(*) FROM memories')
        categories = await conn.fetch('SELECT category, COUNT(*) as cnt FROM memories GROUP BY category')
        logger.info(f"\n✅ Migration complete! Total: {count} memories")
        for row in categories:
            logger.info(f"   {row['category']}: {row['cnt']}")
        
        await conn.close()
        
    except Exception as e:
        logger.error(f"Migration failed: {e}", exc_info=True)
        raise

if __name__ == '__main__':
    asyncio.run(main())
