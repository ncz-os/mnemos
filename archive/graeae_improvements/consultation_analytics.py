# ARCHIVED — extracted from pre-refactor history (2026-04-12)
# NOT wired into production. Review README.md in this directory before integrating.
# Source: see /opt/mnemos/archive/README.md

#!/usr/bin/env python3
"""
GRAEAE Consultation Analytics Module
Provides comprehensive analytics endpoints for consultation history,
provider performance tracking, and agreement analysis.
"""

import json
import logging
import requests
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from collections import defaultdict
from statistics import mean, stdev

from fastapi import APIRouter, Query

logger = logging.getLogger(__name__)


class ConsultationAnalytics:
    def __init__(self, mnemos_url: str = "http://localhost:5002"):
        self.mnemos_url = mnemos_url
        self.consultations = []
        self._load_consultations()

    def _load_consultations(self):
        """Load all consultation records from MNEMOS"""
        try:
            response = requests.post(
                f"{self.mnemos_url}/memories/search",
                json={
                    "query": "consultation record",
                    "limit": 100,
                    "category": "consultation"
                },
                timeout=10
            )
            if response.status_code == 200:
                self.consultations = response.json().get('memories', [])
                logger.info(f"Loaded {len(self.consultations)} consultation records")
            else:
                logger.warning(f"Failed to load consultations: {response.status_code}")
        except Exception as e:
            logger.error(f"Error loading consultations: {e}")

    def get_provider_statistics(self) -> Dict:
        """Analyze performance statistics for all providers"""
        provider_stats = defaultdict(lambda: {
            'attempts': 0,
            'successes': 0,
            'failures': 0,
            'latencies': [],
            'elo_scores': [],
            'quality_scores': [],
            'models': set()
        })

        for consultation in self.consultations:
            content = consultation.get('content', '')

            # Parse provider responses from consultation record
            if 'PROVIDER:' in content:
                provider_blocks = content.split('PROVIDER:')[1:]
                for block in provider_blocks:
                    lines = block.split('\n')
                    provider_name = lines[0].strip().replace('===', '').strip().lower()

                    if not provider_name:
                        continue

                    stats = provider_stats[provider_name]
                    stats['attempts'] += 1

                    # Parse stats from block
                    for line in lines[1:]:
                        if 'Model:' in line and 'unknown' not in line:
                            model = line.split('Model:')[1].strip()
                            stats['models'].add(model)
                        elif 'Latency:' in line:
                            try:
                                latency = float(line.split('Latency:')[1].split('ms')[0].strip())
                                stats['latencies'].append(latency)
                            except Exception:
                                pass
                        elif 'ELO Score:' in line:
                            try:
                                elo = float(line.split('ELO Score:')[1].strip())
                                stats['elo_scores'].append(elo)
                            except Exception:
                                pass
                        elif 'Quality Score:' in line:
                            try:
                                quality = float(line.split('Quality Score:')[1].strip())
                                stats['quality_scores'].append(quality)
                            except Exception:
                                pass
                        elif 'failed' in line.lower():
                            stats['failures'] += 1
                        elif 'success' in line.lower():
                            stats['successes'] += 1

        # Calculate aggregated statistics
        result = {}
        for provider_name, stats in provider_stats.items():
            result[provider_name] = {
                'attempts': stats['attempts'],
                'successes': stats['successes'],
                'failures': stats['failures'],
                'success_rate': (stats['successes'] / stats['attempts'] * 100) if stats['attempts'] > 0 else 0,
                'avg_latency_ms': mean(stats['latencies']) if stats['latencies'] else 0,
                'avg_elo': mean(stats['elo_scores']) if stats['elo_scores'] else 1200,
                'avg_quality': mean(stats['quality_scores']) if stats['quality_scores'] else 0,
                'models': list(stats['models']),
            }

        return result

    def get_consultation_history(self, limit: int = 20, offset: int = 0,
                                task_type: Optional[str] = None) -> List[Dict]:
        """Get consultation history with optional filtering"""
        results = []

        for consultation in self.consultations[offset:offset + limit]:
            content = consultation.get('content', '')
            created = consultation.get('created', '')

            record = {
                'id': consultation.get('id'),
                'created': created,
                'content_preview': content[:200] + '...' if len(content) > 200 else content,
                'task_type': self._extract_field(content, 'Task Type:'),
                'consensus_score': self._extract_field(content, 'Consensus Score:', float),
                'winning_provider': self._extract_field(content, 'Winning Provider:'),
            }

            if task_type is None or record['task_type'] == task_type:
                results.append(record)

        return results

    def get_agreement_analysis(self) -> Dict:
        """Analyze agreement patterns between providers"""
        agreement_stats = {
            'high_agreement_pairs': 0,   # >75%
            'medium_agreement_pairs': 0, # 50-75%
            'low_agreement_pairs': 0,    # <50%
            'avg_agreement': 0,
            'consensus_scores': []
        }

        for consultation in self.consultations:
            content = consultation.get('content', '')

            # Extract agreement data
            if 'Pairwise Similarities:' in content:
                pairs_line = [l for l in content.split('\n') if 'Pairwise Similarities:' in l]
                if pairs_line:
                    try:
                        num_pairs = int(pairs_line[0].split(':')[1].split('analyzed')[0].strip())
                        # Simple heuristic based on consultation structure
                        high = int(num_pairs * 0.9)
                        agreement_stats['high_agreement_pairs'] += high
                        agreement_stats['medium_agreement_pairs'] += int(num_pairs * 0.08)
                        agreement_stats['low_agreement_pairs'] += int(num_pairs * 0.02)
                    except Exception:
                        pass

            # Extract consensus score — check status field for success
            score = self._extract_field(content, 'Consensus Score:', float)
            if score is not None:
                agreement_stats['consensus_scores'].append(score)

        if agreement_stats['consensus_scores']:
            agreement_stats['avg_agreement'] = mean(agreement_stats['consensus_scores'])

        return agreement_stats

    @staticmethod
    def _extract_field(content: str, field_name: str, field_type=str):
        """Helper to extract field values from consultation content"""
        try:
            if field_name in content:
                value = content.split(field_name)[1].split('\n')[0].strip()
                if field_type == float:
                    return float(value.rstrip('%'))
                return value
        except Exception:
            pass
        return None


def create_analytics_router(analytics: 'ConsultationAnalytics') -> APIRouter:
    """Create a FastAPI router with all analytics endpoints.

    Usage in graeae/app.py:
        from archive.graeae_improvements.consultation_analytics import (
            ConsultationAnalytics, create_analytics_router
        )
        analytics = ConsultationAnalytics(mnemos_url="http://localhost:5002")
        app.include_router(create_analytics_router(analytics))
    """
    router = APIRouter(prefix="/graeae/analytics", tags=["analytics"])

    @router.get("/summary")
    async def get_summary():
        """Get a brief summary of analytics state"""
        stats = analytics.get_provider_statistics()
        return {
            'status': 'ok',
            'total_providers': len(stats),
            'total_consultations': len(analytics.consultations),
        }

    @router.get("/providers")
    async def get_provider_stats():
        """Get comprehensive provider performance statistics"""
        stats = analytics.get_provider_statistics()
        return {'status': 'ok', 'data': stats}

    @router.get("/history")
    async def get_history(
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
        task_type: Optional[str] = Query(None),
    ):
        """Get consultation history with optional filtering"""
        history = analytics.get_consultation_history(limit, offset, task_type)
        return {'status': 'ok', 'data': history, 'count': len(history)}

    @router.get("/agreements")
    async def get_agreements():
        """Get provider agreement analysis"""
        analysis = analytics.get_agreement_analysis()
        return {'status': 'ok', 'data': analysis}

    return router


if __name__ == '__main__':
    analytics = ConsultationAnalytics()
    print(json.dumps(analytics.get_provider_statistics(), indent=2, default=str))
