# ARCHIVED — extracted from pre-refactor history (2026-04-12)
# NOT wired into production. Review README.md in this directory before integrating.
# Source: see /opt/mnemos/archive/README.md

#!/usr/bin/env python3
"""
Git History Distillation Job
Extracts architectural knowledge from git commits and stores in MNEMOS
"""

import subprocess
import json
import re
import sys
from datetime import datetime
from typing import List, Dict, Optional

class GitDistiller:
    """Extract architectural facts from git history"""
    
    def __init__(self, repo_path: str):
        self.repo_path = repo_path
        self.commits = []
        self.facts = []
    
    def get_commit_history(self, limit: int = 100) -> List[Dict]:
        """Fetch commit history from repository"""
        try:
            cmd = [
                "git", "-C", self.repo_path,
                "log", "--format=%H|%an|%ae|%ad|%s|%b",
                "--date=short", f"-{limit}"
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            
            if result.returncode != 0:
                print(f"Error fetching git log: {result.stderr}", file=sys.stderr)
                return []
            
            commits = []
            for line in result.stdout.strip().split('\n'):
                if not line:
                    continue
                parts = line.split('|', 4)
                if len(parts) >= 5:
                    commits.append({
                        'hash': parts[0][:8],
                        'author': parts[1],
                        'email': parts[2],
                        'date': parts[3],
                        'subject': parts[4],
                        'body': parts[5] if len(parts) > 5 else ''
                    })
            
            return commits
        except Exception as e:
            print(f"Error getting commit history: {e}", file=sys.stderr)
            return []
    
    def categorize_commit(self, commit: Dict) -> str:
        """Determine commit type from message"""
        subject = commit['subject'].lower()
        
        if subject.startswith('fix'):
            return 'bugfix'
        elif subject.startswith('feat'):
            return 'feature'
        elif subject.startswith('refactor'):
            return 'refactor'
        elif subject.startswith('perf'):
            return 'optimization'
        elif subject.startswith('docs'):
            return 'documentation'
        elif subject.startswith('test'):
            return 'testing'
        else:
            return 'update'
    
    def extract_architectural_concepts(self, text: str) -> List[str]:
        """Extract architectural patterns from commit message/body"""
        concepts = []
        
        patterns = {
            'dual-write': r'dual.?write|redundant.*write|fallback',
            'caching': r'cache|memoization|buffering',
            'background-job': r'background.*job|async.*process|daemon',
            'failover': r'failover|fallback|redundancy',
            'api-design': r'endpoint|api|rest|graphql',
            'database': r'postgresql|database|sql|query|schema',
            'docker': r'docker|container|image|build',
            'performance': r'performance|optimization|throughput|latency',
            'reliability': r'reliability|robustness|fault.*tolerance',
            'integration': r'integration|plugin|module',
        }
        
        text_lower = text.lower()
        for concept, pattern in patterns.items():
            if re.search(pattern, text_lower):
                concepts.append(concept)
        
        return concepts
    
    def extract_facts(self, commits: Optional[List[Dict]] = None) -> List[Dict]:
        """Extract facts from commits"""
        if commits is None:
            commits = self.commits
        
        facts = []
        
        for commit in commits:
            commit_type = self.categorize_commit(commit)
            full_text = commit['subject'] + ' ' + commit['body']
            concepts = self.extract_architectural_concepts(full_text)
            
            # Create memory-ready fact
            fact = {
                'content': f"{commit['subject']}\n\n{commit['body'][:200]}..." if commit['body'] else commit['subject'],
                'category': 'git_artifacts',
                'tags': [commit_type] + concepts,
                'metadata': {
                    'commit_hash': commit['hash'],
                    'author': commit['author'],
                    'date': commit['date'],
                    'type': commit_type,
                    'concepts': concepts
                },
                'created': commit['date']
            }
            
            # Remove empty/None fields
            fact = {k: v for k, v in fact.items() if v}
            facts.append(fact)
        
        return facts
    
    def run(self, limit: int = 50) -> List[Dict]:
        """Extract facts from git history"""
        print(f"[GIT-DISTILL] Fetching last {limit} commits from {self.repo_path}...")
        commits = self.get_commit_history(limit)
        print(f"[GIT-DISTILL] Found {len(commits)} commits")
        
        if not commits:
            return []
        
        print(f"[GIT-DISTILL] Extracting architectural facts...")
        facts = self.extract_facts(commits)
        print(f"[GIT-DISTILL] Extracted {len(facts)} facts")
        
        for i, fact in enumerate(facts[:3]):
            print(f"\n[GIT-DISTILL] Sample fact {i+1}:")
            print(f"  Content: {fact['content'][:80]}...")
            print(f"  Tags: {fact.get('tags', [])}")
            print(f"  Commit: {fact['metadata']['commit_hash']} ({fact['metadata']['date']})")
        
        return facts

if __name__ == '__main__':
    import sys
    
    repo_path = sys.argv[1] if len(sys.argv) > 1 else '/home/jasonperlow/mnemos-api-production'
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 50
    
    distiller = GitDistiller(repo_path)
    facts = distiller.run(limit)
    
    # Output as JSON for integration with MNEMOS
    print(f"\n[GIT-DISTILL] Output (JSON):")
    print(json.dumps(facts, indent=2))
