# ARCHIVED — extracted from pre-refactor history (2026-04-12)
# NOT wired into production. Review README.md in this directory before integrating.
# Source: see /opt/mnemos/archive/README.md

#!/usr/bin/env python3
"""
Git History to MNEMOS Integration
Extracts git facts and stores them in MNEMOS memory system
"""

import sys
import json
import requests
import subprocess
from datetime import datetime

class GitToMNEMOS:
    """Bridge git distillation with MNEMOS memory storage"""
    
    def __init__(self, repo_path: str, mnemos_url: str = "http://192.0.2.67:5000"):
        self.repo_path = repo_path
        self.mnemos_url = mnemos_url
        self.facts_stored = 0
        self.facts_failed = 0
    
    def run_distiller(self, limit: int = 50) -> list:
        """Run git distillation and get facts"""
        try:
            result = subprocess.run(
                ["python3", f"{self.repo_path}/git_distillation_job.py", self.repo_path, str(limit)],
                capture_output=True,
                text=True,
                timeout=30
            )
            
            if result.returncode != 0:
                print(f"[GIT→MNEMOS] Distillation failed: {result.stderr}", file=sys.stderr)
                return []
            
            # Extract JSON from output (skip log lines that start with [GIT-DISTILL])
            lines = result.stdout.strip().split('\n')
            json_start = None

            # Find the first line that starts with '[' but is NOT a log line
            for i, line in enumerate(lines):
                stripped = line.strip()
                if stripped.startswith('[') and not stripped.startswith('[GIT-DISTILL]'):
                    json_start = i
                    break

            if json_start is None:
                print("[GIT→MNEMOS] No JSON output from distiller", file=sys.stderr)
                return []

            json_str = '\n'.join(lines[json_start:])
            facts = json.loads(json_str)
            print(f"[GIT→MNEMOS] Extracted {len(facts)} facts from git")
            return facts
            
        except Exception as e:
            print(f"[GIT→MNEMOS] Error running distiller: {e}", file=sys.stderr)
            return []
    
    def store_facts(self, facts: list) -> int:
        """Store extracted facts in MNEMOS"""
        stored = 0
        
        for fact in facts:
            try:
                # Format for MNEMOS
                payload = {
                    "content": fact['content'],
                    "category": fact.get('category', 'git_artifacts'),
                    "metadata": {
                        **fact.get('metadata', {}),
                        "imported_from": "git_distillation",
                        "import_date": datetime.now().isoformat()
                    }
                }
                
                resp = requests.post(
                    f"{self.mnemos_url}/memories",
                    json=payload,
                    timeout=10
                )
                
                if resp.status_code in (200, 201):
                    result = resp.json()
                    memory_id = result.get('id')
                    print(f"  ✓ Stored: {fact['metadata']['commit_hash']} → {memory_id}")
                    stored += 1
                else:
                    print(f"  ✗ Failed to store {fact['metadata']['commit_hash']}: {resp.status_code}")
                    self.facts_failed += 1
                    
            except Exception as e:
                print(f"  ✗ Error storing fact: {e}", file=sys.stderr)
                self.facts_failed += 1
        
        return stored
    
    def run(self, limit: int = 50) -> bool:
        """Complete workflow: distill and store"""
        print(f"\n[GIT→MNEMOS] Starting git history distillation and storage...")
        print(f"  Repository: {self.repo_path}")
        print(f"  MNEMOS: {self.mnemos_url}")
        print(f"  Commit limit: {limit}")
        
        # Verify MNEMOS is accessible
        try:
            resp = requests.get(f"{self.mnemos_url}/health", timeout=5)
            if resp.status_code != 200:
                print(f"[GIT→MNEMOS] MNEMOS health check failed: {resp.status_code}", file=sys.stderr)
                return False
            print("  ✓ MNEMOS is accessible")
        except Exception as e:
            print(f"[GIT→MNEMOS] Cannot reach MNEMOS: {e}", file=sys.stderr)
            return False
        
        # Run distillation
        facts = self.run_distiller(limit)
        if not facts:
            print("[GIT→MNEMOS] No facts to store", file=sys.stderr)
            return False
        
        # Store facts
        print(f"\n[GIT→MNEMOS] Storing {len(facts)} facts in MNEMOS...")
        stored = self.store_facts(facts)
        
        # Summary
        print(f"\n[GIT→MNEMOS] Complete!")
        print(f"  Stored: {stored}/{len(facts)}")
        if self.facts_failed > 0:
            print(f"  Failed: {self.facts_failed}")
        
        return stored > 0

if __name__ == '__main__':
    repo = sys.argv[1] if len(sys.argv) > 1 else '/home/jasonperlow/mnemos-api-production'
    mnemos = sys.argv[2] if len(sys.argv) > 2 else 'http://192.0.2.67:5000'
    limit = int(sys.argv[3]) if len(sys.argv) > 3 else 50
    
    integrator = GitToMNEMOS(repo, mnemos)
    success = integrator.run(limit)
    sys.exit(0 if success else 1)
