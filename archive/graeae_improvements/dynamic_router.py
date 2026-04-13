# ARCHIVED — extracted from pre-refactor history (2026-04-12)
# NOT wired into production. Review README.md in this directory before integrating.
# Source: see /opt/mnemos/archive/README.md

"""
DynamicRouter for GRAEAE - Intelligent provider selection

Integration note: To wire this into the production GRAEAE FastAPI app, import
DynamicRouter in graeae/app.py, instantiate it at startup, call select_model()
before dispatching to providers, and call record_inference() after each response.
The _save_metrics() method performs file I/O; in an async context wrap it with
asyncio.get_event_loop().run_in_executor(None, self._save_metrics) to avoid
blocking the event loop.
"""

import importlib.util
import json
import logging
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from collections import defaultdict
import statistics

logger = logging.getLogger(__name__)

# Load FitnessCalculator from the same directory without sys.path manipulation
_here = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "fitness_calculator", os.path.join(_here, "fitness_calculator.py")
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
FitnessCalculator = _mod.FitnessCalculator


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
            except Exception as exc:
                logger.warning(f"Could not load metrics from {self.metrics_file}: {exc}")
                return defaultdict(lambda: {'latencies': [], 'costs': [], 'errors': 0, 'uptime': 99.9})
        return defaultdict(lambda: {'latencies': [], 'costs': [], 'errors': 0, 'uptime': 99.9})

    def _save_metrics(self):
        """Persist metrics to disk.

        Note: this does synchronous file I/O. In an async context wrap with
        asyncio.get_event_loop().run_in_executor(None, self._save_metrics).
        """
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
    """Intelligent routing engine using FitnessCalculator.

    Integration note: To wire into production graeae/app.py, instantiate this
    class at app startup with the path to your routing config JSON. Call
    select_model() before dispatching to providers and record_inference() after
    each completed response. See module docstring for async I/O notes.
    """

    def __init__(self, config_file: str, metrics_file: str = '/tmp/graeae_router_metrics.json'):
        self.config_file = config_file
        self.performance_tracker = PerformanceTracker(metrics_file)
        self.config = self._load_config()
        self.calculator = FitnessCalculator()

    def _load_config(self) -> Dict:
        """Load routing config"""
        try:
            with open(self.config_file, 'r') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading config from {self.config_file}: {e}")
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

        if not ranked:
            return {'error': f'No models configured for task: {task_type}'}

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


if __name__ == '__main__':
    # Demo — uses a minimal inline config instead of a file on disk
    import json, tempfile

    demo_config = {
        "task_routing": {
            "reasoning": {
                "premium": {
                    "primary": {"name": "Grok Reasoning", "provider": "xai", "elo": 1500, "cost": 0.02, "model_id": "grok-4.20-0309-reasoning"},
                    "fallback_1": {"name": "GPT-4.1 Nano", "provider": "openai", "elo": 1480, "cost": 0.015, "model_id": "gpt-4.1-nano"},
                },
                "balanced": {
                    "primary": {"name": "Groq LLaMA", "provider": "groq", "elo": 1450, "cost": 0.002, "model_id": "llama-3.3-70b-versatile"},
                    "fallback_1": {"name": "GPT-4.1 Nano", "provider": "openai", "elo": 1480, "cost": 0.015, "model_id": "gpt-4.1-nano"},
                },
            },
            "speed": {
                "cheap": {
                    "primary": {"name": "Groq LLaMA", "provider": "groq", "elo": 1450, "cost": 0.002, "model_id": "llama-3.3-70b-versatile"},
                    "fallback_1": {"name": "Grok Reasoning", "provider": "xai", "elo": 1500, "cost": 0.02, "model_id": "grok-4.20-0309-reasoning"},
                },
            },
        }
    }

    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(demo_config, f)
        tmp_path = f.name

    router = DynamicRouter(config_file=tmp_path)

    print("=" * 70)
    print("DYNAMIC ROUTER DEMO")
    print("=" * 70)

    scenarios = [
        ('speed', 'cheap'),
        ('reasoning', 'premium'),
        ('reasoning', 'balanced'),
    ]

    for task_type, budget in scenarios:
        print(f"\nTask: {task_type.upper()} | Budget: {budget.upper()}")
        print("-" * 70)

        result = router.select_model(task_type, budget)

        if 'error' in result:
            print(f"  Error: {result['error']}")
            continue

        print(f"Selected: {result['selected_model']['name']}")
        print(f"   Model ID: {result['selected_model']['model_id']}")
        print(f"   Provider: {result['selected_model']['provider']}")
        print(f"   Fitness: {result['confidence']:.2f}")
        print(f"   Reason: {result['selection_reason']}")

        print(f"\n   All candidates:")
        for item in result['ranked_selection']:
            print(f"     #{item['rank']}: {item['name']:20} fitness={item['fitness']:.2f}")
