# ARCHIVED — extracted from pre-refactor history (2026-04-12)
# NOT wired into production. Review README.md in this directory before integrating.
# Source: see /opt/mnemos/archive/README.md

#!/usr/bin/env python3
"""
Git History to MNEMOS Integration
Extracts git facts and stores them in MNEMOS memory system
"""

import os
import sys
import json
import requests
import subprocess
from datetime import datetime

class GitToMNEMOS:
    """Bridge git distillation with MNEMOS memory storage"""

    def __init__(self, repo_path: str, mnemos_url: str = "http://localhost:5002"):
        self.repo_path = repo_path
        self.mnemos_url = mnemos_url
        self.facts_stored = 0
        self.facts_failed = 0

    def run_distiller(self, limit: int = 50) -> list:
        """Run git distillation and get facts"""
        try:
            distill_script = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), 'git_distillation_job.py'
            )
            result = subprocess.run(
                ["python3", distill_script, self.repo_path, str(limit)],
                capture_output=True,
                text=True,
                timeout=30
            )

            if result.returncode != 0:
                print(f"[GIT->MNEMOS] Distillation failed: {result.stderr}", file=sys.stderr)
                return []

            # Robustly extract the last JSON array from stdout
            output = result.stdout
            for i in range(len(output) - 1, -1, -1):
                if output[i] == '[':
                    try:
                        facts = json.loads(output[i:])
                        print(f"[GIT->MNEMOS] Extracted {len(facts)} facts from git")
                        return facts
                    except json.JSONDecodeError:
                        continue

            print("[GIT->MNEMOS] No JSON output from distiller", file=sys.stderr)
            return []

        except Exception as e:
            print(f"[GIT->MNEMOS] Error running distiller: {e}", file=sys.stderr)
            return []

    def store_facts(self, facts: list) -> int:
        """Store extracted facts in MNEMOS, skipping already-stored commits"""
        stored = 0

        for fact in facts:
            try:
                commit_hash = fact.get('metadata', {}).get('commit_hash', 'unknown')

                # Deduplication: skip if already stored
                try:
                    search_resp = requests.post(
                        f"{self.mnemos_url}/memories/search",
                        json={"query": commit_hash, "limit": 1},
                        timeout=5
                    )
                    if search_resp.status_code == 200:
                        results = search_resp.json().get('results', [])
                        if results and results[0].get('score', 0) > 0.99:
                            print(f"  ~ Skipping {commit_hash} (already stored)")
                            continue
                except Exception:
                    pass  # dedup failure is non-fatal; proceed with store attempt

                # Format for MNEMOS
                payload = {
                    "content": fact['content'],
                    "category": fact.get('category', 'decisions'),
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
                    print(f"  + Stored: {commit_hash} -> {memory_id}")
                    self.facts_stored += 1
                    stored += 1
                else:
                    print(f"  x Failed to store {commit_hash}: {resp.status_code}")
                    self.facts_failed += 1

            except Exception as e:
                print(f"  x Error storing fact: {e}", file=sys.stderr)
                self.facts_failed += 1

        return stored

    def run(self, limit: int = 50) -> bool:
        """Complete workflow: distill and store"""
        print(f"\n[GIT->MNEMOS] Starting git history distillation and storage...")
        print(f"  Repository: {self.repo_path}")
        print(f"  MNEMOS: {self.mnemos_url}")
        print(f"  Commit limit: {limit}")

        # Verify MNEMOS is accessible
        try:
            resp = requests.get(f"{self.mnemos_url}/health", timeout=5)
            if resp.status_code != 200:
                print(f"[GIT->MNEMOS] MNEMOS health check failed: {resp.status_code}", file=sys.stderr)
                return False
            print("  + MNEMOS is accessible")
        except Exception as e:
            print(f"[GIT->MNEMOS] Cannot reach MNEMOS: {e}", file=sys.stderr)
            return False

        # Run distillation
        facts = self.run_distiller(limit)
        if not facts:
            print("[GIT->MNEMOS] No facts to store", file=sys.stderr)
            return False

        # Store facts
        print(f"\n[GIT->MNEMOS] Storing {len(facts)} facts in MNEMOS...")
        stored = self.store_facts(facts)

        # Summary
        print(f"\n[GIT->MNEMOS] Complete!")
        print(f"  Stored: {stored}/{len(facts)}")
        if self.facts_failed > 0:
            print(f"  Failed: {self.facts_failed}")

        return stored > 0

if __name__ == '__main__':
    repo = sys.argv[1] if len(sys.argv) > 1 else '/opt/mnemos'
    mnemos = sys.argv[2] if len(sys.argv) > 2 else 'http://localhost:5002'
    limit = int(sys.argv[3]) if len(sys.argv) > 3 else 50

    integrator = GitToMNEMOS(repo, mnemos)
    success = integrator.run(limit)
    sys.exit(0 if success else 1)
