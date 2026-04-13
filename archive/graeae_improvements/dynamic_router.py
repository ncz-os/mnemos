# ARCHIVED — extracted from pre-refactor history (2026-04-12)
# NOT wired into production. Review README.md in this directory before integrating.
# Source: see /opt/mnemos/archive/README.md

"""
DynamicRouter for GRAEAE - Intelligent provider selection
"""

import json
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from collections import defaultdict
import statistics

class PerformanceTracker:
    """Tracks latency and cost metrics"""
    
    def __init__(self, metrics_file: str = '/tmp/graeae_router_metrics.json'):
        self.metrics_file = metrics_file
        self.metrics = self._load_metrics()
    
    def _load_metrics(self) -> Dict:
        """Load metrics from disk"""
        if os.path.exists(self.metrics_file):
            try:
                with open(self.metrics_file, 'r') as f:
                    return json.load(f)
            except:
                return defaultdict(lambda: {'latencies': [], 'costs': [], 'errors': 0, 'uptime': 99.9})
        return defaultdict(lambda: {'latencies': [], 'costs': [], 'errors': 0, 'uptime': 99.9})
    
    def _save_metrics(self):
        """Persist metrics to disk"""
        with open(self.metrics_file, 'w') as f:
            json.dump(dict(self.metrics), f, indent=2)
    
    def record(self, model_id: str, latency_ms: float, cost: float, success: bool = True):
        """Record a single inference"""
        if model_id not in self.metrics:
            self.metrics[model_id] = {'latencies': [], 'costs': [], 'errors': 0, 'uptime': 99.9}
        
        # Keep only last 1000 samples (24hr window)
        if len(self.metrics[model_id]['latencies']) >= 1000:
            self.metrics[model_id]['latencies'].pop(0)
            self.metrics[model_id]['costs'].pop(0)
        
        self.metrics[model_id]['latencies'].append(latency_ms)
        self.metrics[model_id]['costs'].append(cost)
        
        if not success:
            self.metrics[model_id]['errors'] += 1
        
        self._save_metrics()
    
    def get_metrics(self, model_id: str) -> Dict:
        """Get current metrics for a model"""
        if model_id not in self.metrics or not self.metrics[model_id]['latencies']:
            return {
                'avg_latency_ms': 5000,  # Default estimate
                'p95_latency_ms': 7000,
                'avg_cost': 0.01,
                'uptime_percent': 99.9,
                'recent_failures': 0
            }
        
        data = self.metrics[model_id]
        latencies = data['latencies'][-100:]  # Last 100 samples
        costs = data['costs'][-100:]
        
        return {
            'avg_latency_ms': statistics.mean(latencies),
            'p95_latency_ms': sorted(latencies)[int(len(latencies) * 0.95)] if latencies else 5000,
            'avg_cost': statistics.mean(costs),
            'uptime_percent': 100.0 - (data['errors'] / max(len(latencies), 1)) * 100,
            'recent_failures': data['errors']
        }


class DynamicRouter:
    """Intelligent routing engine using FitnessCalculator"""
    
    def __init__(self, config_file: str, metrics_file: str = '/tmp/graeae_router_metrics.json'):
        self.config_file = config_file
        self.performance_tracker = PerformanceTracker(metrics_file)
        self.config = self._load_config()
        
        # Import fitness calculator
        import sys
        sys.path.insert(0, '/tmp')
        from fitness_calculator import FitnessCalculator
        self.calculator = FitnessCalculator()
    
    def _load_config(self) -> Dict:
        """Load routing config"""
        try:
            with open(self.config_file, 'r') as f:
                return json.load(f)
        except Exception as e:
            print(f"Error loading config: {e}")
            return {}
    
    def select_model(self, 
                    task_type: str,
                    budget_constraint: str = 'balanced',
                    context: Dict = None) -> Dict:
        """
        Select best model for task using dynamic routing
        
        Returns:
            {
                'selected_model': model_dict,
                'primary': primary_model,
                'fallback_1': fallback_1_model,
                'fallback_2': fallback_2_model,
                'fitness_scores': {model_id: score},
                'selection_reason': explanation,
                'confidence': 0.0-1.0
            }
        """
        
        # Get bundle from config
        if task_type not in self.config.get('task_routing', {}):
            return {'error': f'Unknown task type: {task_type}'}
        
        if budget_constraint not in self.config['task_routing'][task_type]:
            budget_constraint = 'balanced'  # Fallback
        
        bundle = self.config['task_routing'][task_type][budget_constraint]
        
        # Extract models from bundle
        models = []
        model_keys = []
        
        for key in ['primary', 'fallback_1', 'fallback_2']:
            if key in bundle:
                model = bundle[key].copy()
                models.append(model)
                model_keys.append(key)
        
        # Score each model with real-time metrics
        metrics_data = {}
        for model in models:
            model_id = model.get('model_id', '')
            metrics_data[model_id] = self.performance_tracker.get_metrics(model_id)
        
        # Rank by fitness
        ranked = self.calculator.rank_models(
            models, task_type, budget_constraint, metrics_data
        )
        
        # Selection results
        results = {
            'task_type': task_type,
            'budget_constraint': budget_constraint,
            'primary': bundle.get('primary'),
            'fallback_1': bundle.get('fallback_1'),
            'fallback_2': bundle.get('fallback_2'),
            'fitness_scores': {},
            'ranked_selection': []
        }
        
        # Build response
        for i, (model, fitness, reasoning) in enumerate(ranked):
            model_id = model.get('model_id', '')
            results['fitness_scores'][model_id] = fitness
            results['ranked_selection'].append({
                'rank': i + 1,
                'model_id': model_id,
                'name': model.get('name'),
                'provider': model.get('provider'),
                'fitness': fitness,
                'reasoning': reasoning
            })
        
        # Best selection (highest fitness)
        best_model, best_fitness, best_reason = ranked[0]
        results['selected_model'] = best_model
        results['selection_reason'] = best_reason
        results['confidence'] = best_fitness
        
        return results
    
    def record_inference(self, model_id: str, latency_ms: float, 
                        cost: float, success: bool = True):
        """Record inference metrics"""
        self.performance_tracker.record(model_id, latency_ms, cost, success)


# Integration with GRAEAE - add this to graeae_api.py
def integrate_into_graeae_api():
    """Code snippet to add to graeae_api.py"""
    
    code = '''
# At top of graeae_api.py
import sys
sys.path.insert(0, '/home/jasonperlow/graeae')
from dynamic_router import DynamicRouter

# In GraaeConsensus.__init__
self.dynamic_router = DynamicRouter(
    config_file=os.path.expanduser('~/graeae/graeae_routing_config.json')
)

# In /graeae/consult endpoint, before calling muses:
if request.json.get('use_dynamic_routing', True):
    router_decision = self.dynamic_router.select_model(
        task_type=task_type,
        budget_constraint=request.json.get('budget', 'balanced'),
        context={'tokens': request.json.get('token_count', 0)}
    )
    response['router_decision'] = router_decision
    # Can modify model selection based on router decision

# After inference completes, track performance:
if hasattr(self, 'dynamic_router'):
    self.dynamic_router.record_inference(
        model_id=winning_model_id,
        latency_ms=latency_ms,
        cost=cost_per_inference
    )
'''
    return code


if __name__ == '__main__':
    # Demo
    router = DynamicRouter(
        config_file=os.path.expanduser('/tmp/graeae_routing_config_optimized_phase1.json')
    )
    
    print("=" * 70)
    print("PHASE 2 DYNAMIC ROUTER DEMO")
    print("=" * 70)
    
    scenarios = [
        ('speed', 'cheap'),
        ('reasoning', 'premium'),
        ('coding', 'balanced'),
        ('fast_categorization', 'free'),
    ]
    
    for task_type, budget in scenarios:
        print(f"\n📊 Task: {task_type.upper()} | Budget: {budget.upper()}")
        print("-" * 70)
        
        result = router.select_model(task_type, budget)
        
        if 'error' in result:
            print(f"  Error: {result['error']}")
            continue
        
        print(f"✅ Selected: {result['selected_model']['name']}")
        print(f"   Model ID: {result['selected_model']['model_id']}")
        print(f"   Provider: {result['selected_model']['provider']}")
        print(f"   Fitness: {result['confidence']:.2f}")
        print(f"   Reason: {result['selection_reason']}")
        
        print(f"\n   All candidates:")
        for item in result['ranked_selection']:
            print(f"     #{item['rank']}: {item['name']:20} fitness={item['fitness']:.2f}")

