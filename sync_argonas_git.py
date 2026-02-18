#!/usr/bin/env python3
import subprocess
import json
import requests
import os
from datetime import datetime
from pathlib import Path

GIT_REPOS = [
    '/mnt/argonas/backups/RiskyEats_Pipeline.git',
    '/mnt/argonas/backups/ETLANTIS_Pipeline.git',
    '/mnt/argonas/backups/MayaFerries.git'
]

MNEMOS_URL = os.getenv('MNEMOS_URL', 'http://localhost:5000')

def get_recent_commits(repo_path, limit=20):
    try:
        result = subprocess.run(
            ['git', '-C', repo_path, 'log', '--oneline', f'-{limit}'],
            capture_output=True, text=True, timeout=10
        )
        commits = []
        for line in result.stdout.strip().split('
'):
            if line:
                hash_val, msg = line.split(' ', 1)
                commits.append({'hash': hash_val, 'message': msg})
        return commits
    except:
        return []

def store_commit(commit, project_name):
    try:
        content = f'[GIT COMMIT] {project_name}

Hash: {commit["hash"][:8]}
Message: {commit["message"]}'
        response = requests.post(
            f'{MNEMOS_URL}/memories',
            json={'content': content, 'category': 'git_commit', 'metadata': {'project': project_name}},
            timeout=5
        )
        return response.status_code == 201
    except:
        return False

def sync_all_repos():
    print(f'[SYNC] Starting git sync')
    total = 0
    for repo_path in GIT_REPOS:
        if not os.path.exists(repo_path):
            continue
        project = Path(repo_path).parent.name
        for commit in get_recent_commits(repo_path, 30):
            if store_commit(commit, project):
                total += 1
    print(f'[SYNC] Synced {total} commits')
    return total

if __name__ == '__main__':
    sync_all_repos()
