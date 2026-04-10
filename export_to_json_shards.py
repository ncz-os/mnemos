#!/usr/bin/env python3
import json
import psycopg
from psycopg.rows import dict_row
from pathlib import Path
from datetime import datetime
import os

DB_CONFIG = {
    'host': os.getenv('PG_HOST', 'localhost'),
    'port': int(os.getenv('PG_PORT', '5432')),
    'database': os.getenv('PG_DATABASE', 'mnemos'),
    'user': os.getenv('PG_USER', 'mnemos_user'),
    'password': os.getenv('PG_PASSWORD', 'mnemos_secure_password')
}

SHARD_DIR = Path.home() / '.mnemos'
BACKUP_DIR = SHARD_DIR / 'backups'
BACKUP_DIR.mkdir(parents=True, exist_ok=True)

def export_memories():
    conn = psycopg.connect(**DB_CONFIG, row_factory=dict_row)
    cur = conn.cursor()
    
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    print(f'[EXPORT] Starting memory export at {timestamp}')
    
    # Get all categories
    cur.execute('SELECT DISTINCT category FROM memories ORDER BY category')
    categories = [row['category'] for row in cur.fetchall()]
    print(f'[EXPORT] Found {len(categories)} categories')
    
    exported_count = 0
    for category in categories:
        cur.execute(
            'SELECT id, content, category, created, metadata FROM memories WHERE category = %s ORDER BY created DESC',
            (category,)
        )
        memories = cur.fetchall()
        
        if not memories:
            continue
        
        export_data = [
            {
                'id': m['id'],
                'content': m['content'],
                'category': m['category'],
                'created': m['created'].isoformat() if m['created'] else None,
                'metadata': m['metadata'] or {}
            }
            for m in memories
        ]
        
        shard_file = SHARD_DIR / f'{category}.json'
        with open(shard_file, 'w') as f:
            json.dump(export_data, f, indent=2, default=str)
        
        print(f'  ✓ {category}: {len(memories)} memories')
        exported_count += len(memories)
    
    # Create timestamped backup
    cur.execute('SELECT id, content, category, created, metadata FROM memories ORDER BY created DESC')
    all_memories = cur.fetchall()
    
    backup_data = [
        {
            'id': m['id'],
            'content': m['content'],
            'category': m['category'],
            'created': m['created'].isoformat() if m['created'] else None,
            'metadata': m['metadata'] or {}
        }
        for m in all_memories
    ]
    
    backup_file = BACKUP_DIR / f'mnemos_backup_{timestamp}.json'
    with open(backup_file, 'w') as f:
        json.dump(backup_data, f, indent=2, default=str)
    
    print(f'[EXPORT] Backup: {backup_file.name}')
    print(f'[EXPORT] Total: {exported_count} memories exported')
    
    cur.close()
    conn.close()
    return exported_count

if __name__ == '__main__':
    try:
        count = export_memories()
        print(f'✅ Successfully exported {count} memories')
    except Exception as e:
        print(f'❌ Export failed: {e}')
        exit(1)
