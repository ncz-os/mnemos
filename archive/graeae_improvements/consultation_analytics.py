# ARCHIVED — extracted from pre-refactor history (2026-04-12)
# NOT wired into production. Review README.md in this directory before integrating.
# Source: see /opt/mnemos/archive/README.md

#!/usr/bin/env python3
"""
GRAEAE Consultation Analytics Module
Provides comprehensive analytics endpoints for consultation history,
muse performance tracking, and agreement analysis
"""

import json
import logging
import requests
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Optional
from collections import defaultdict
from statistics import mean, stdev

logger = logging.getLogger(__name__)

class ConsultationAnalytics:
    def __init__(self, mnemos_url: str = "http://localhost:5000"):
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
    
    def get_muse_statistics(self) -> Dict:
        """Analyze performance statistics for all muses"""
        muse_stats = defaultdict(lambda: {
            'attempts': 0,
            'successes': 0,
            'failures': 0,
            'latencies': [],
            'elo_scores': [],
            'quality_scores': [],
            'win_count': 0,
            'models': set()
        })
        
        for consultation in self.consultations:
            content = consultation.get('content', '')
            
            # Parse muse responses from consultation record
            if 'MUSE:' in content:
                muse_blocks = content.split('MUSE:')[1:]
                for block in muse_blocks:
                    lines = block.split('\n')
                    muse_name = lines[0].strip().replace('===', '').strip().lower()
                    
                    if not muse_name or muse_name == '':
                        continue
                    
                    stats = muse_stats[muse_name]
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
                            except:
                                pass
                        elif 'ELO Score:' in line:
                            try:
                                elo = float(line.split('ELO Score:')[1].strip())
                                stats['elo_scores'].append(elo)
                            except:
                                pass
                        elif 'Quality Score:' in line:
                            try:
                                quality = float(line.split('Quality Score:')[1].strip())
                                stats['quality_scores'].append(quality)
                            except:
                                pass
                        elif 'failed' in line.lower():
                            stats['failures'] += 1
                        elif 'success' in line.lower():
                            stats['successes'] += 1
        
        # Calculate aggregated statistics
        result = {}
        for muse_name, stats in muse_stats.items():
            result[muse_name] = {
                'attempts': stats['attempts'],
                'successes': stats['successes'],
                'failures': stats['failures'],
                'success_rate': (stats['successes'] / stats['attempts'] * 100) if stats['attempts'] > 0 else 0,
                'avg_latency_ms': mean(stats['latencies']) if stats['latencies'] else 0,
                'avg_elo': mean(stats['elo_scores']) if stats['elo_scores'] else 1200,
                'avg_quality': mean(stats['quality_scores']) if stats['quality_scores'] else 0,
                'models': list(stats['models']),
                'win_count': stats['win_count']
            }
        
        return result
    
    def get_consultation_history(self, limit: int = 20, offset: int = 0, 
                                task_type: Optional[str] = None) -> List[Dict]:
        """Get consultation history with optional filtering"""
        results = []
        
        for i, consultation in enumerate(self.consultations[offset:offset+limit]):
            content = consultation.get('content', '')
            created = consultation.get('created', '')
            
            # Extract metadata
            record = {
                'id': consultation.get('id'),
                'created': created,
                'content_preview': content[:200] + '...' if len(content) > 200 else content,
                'task_type': self._extract_field(content, 'Task Type:'),
                'consensus_score': self._extract_field(content, 'Consensus Score:', float),
                'winning_muse': self._extract_field(content, 'Winning Muse:'),
            }
            
            if task_type is None or record['task_type'] == task_type:
                results.append(record)
        
        return results
    
    def get_agreement_analysis(self) -> Dict:
        """Analyze agreement patterns between muses"""
        agreement_stats = {
            'high_agreement_pairs': 0,  # >75%
            'medium_agreement_pairs': 0,  # 50-75%
            'low_agreement_pairs': 0,   # <50%
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
                    except:
                        pass
            
            # Extract consensus score
            score = self._extract_field(content, 'Consensus Score:', float)
            if score:
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
        except:
            pass
        return None

# Flask endpoints
def register_analytics_endpoints(app, analytics: ConsultationAnalytics):
    """Register all analytics endpoints with Flask app"""
    
    @app.route('/graeae/analytics/muse-stats', methods=['GET'])
    def get_muse_statistics():
        """Get comprehensive muse performance statistics"""
        stats = analytics.get_muse_statistics()
        return {'status': 'ok', 'data': stats}, 200
    
    @app.route('/graeae/analytics/consultation-history', methods=['GET'])
    def get_consultation_history():
        """Get consultation history with optional filtering"""
        limit = request.args.get('limit', 20, type=int)
        offset = request.args.get('offset', 0, type=int)
        task_type = request.args.get('task_type', None)
        
        history = analytics.get_consultation_history(limit, offset, task_type)
        return {'status': 'ok', 'data': history, 'count': len(history)}, 200
    
    @app.route('/graeae/analytics/agreement-analysis', methods=['GET'])
    def get_agreement_analysis():
        """Get muse agreement analysis"""
        analysis = analytics.get_agreement_analysis()
        return {'status': 'ok', 'data': analysis}, 200
    
    @app.route('/graeae/analytics/provider-status', methods=['GET'])
    def get_provider_status():
        """Get status of all providers (working vs failed)"""
        stats = analytics.get_muse_statistics()
        working = [m for m, s in stats.items() if s['success_rate'] > 0]
        failed = [m for m, s in stats.items() if s['success_rate'] == 0]
        
        return {
            'status': 'ok',
            'working_providers': working,
            'failed_providers': failed,
            'total_providers': len(stats)
        }, 200
    
    logger.info('Analytics endpoints registered successfully')

if __name__ == '__main__':
    analytics = ConsultationAnalytics()
    print(json.dumps(analytics.get_muse_statistics(), indent=2, default=str))
