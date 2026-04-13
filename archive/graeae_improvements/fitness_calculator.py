# ARCHIVED — extracted from pre-refactor history (2026-04-12)
# NOT wired into production. Review README.md in this directory before integrating.
# Source: see /opt/mnemos/archive/README.md

"""
Fitness Calculator for GRAEAE Dynamic Routing
Scores models based on Elo, latency, cost, and task requirements
"""

import json
from typing import Dict, List, Tuple
from dataclasses import dataclass
from datetime import datetime

@dataclass
class ModelMetrics:
    """Real-time metrics for a model"""
    model_id: str
    provider: str
    elo_score: float
    avg_latency_ms: float
    cost_per_request: float
    uptime_percent: float = 99.9
    recent_failures: int = 0

class FitnessCalculator:
    """Intelligently scores models for task routing"""
    
    def __init__(self, config: Dict = None):
        self.config = config or {}
        
        # Weight distribution (sum = 1.0)
        self.weights = {
            'elo': 0.30,           # Model quality
            'latency': 0.25,       # Response speed
            'cost': 0.20,          # Budget efficiency
            'reliability': 0.15,   # Uptime/stability
            'availability': 0.10   # Provider load
        }
        
        # Task-specific weight adjustments
        self.task_adjustments = {
            'reasoning': {'elo': 1.5, 'latency': 0.5},      # Prioritize quality
            'research': {'elo': 1.4, 'latency': 0.6},
            'speed': {'latency': 1.5, 'elo': 0.8},          # Prioritize latency
            'fast_categorization': {'latency': 2.0, 'elo': 0.7},
            'coding': {'elo': 1.2, 'latency': 1.0},         # Balanced
            'analysis': {'elo': 1.3, 'latency': 0.9},
            'balanced': {},                                   # Default weights
            'budget': {'cost': 2.0, 'elo': 0.9}             # Prioritize cost
        }
    
    def calculate_fitness(self, 
                         model: Dict,
                         task_type: str,
                         budget_constraint: str,
                         metrics_data: Dict = None) -> float:
        """
        Calculate fitness score (0.0-1.0) for a model given a task
        
        Args:
            model: Model config dict with name, elo, cost, model_id
            task_type: 'reasoning', 'coding', 'speed', etc.
            budget_constraint: 'free', 'cheap', 'balanced', 'premium'
            metrics_data: Optional real-time metrics {model_id: {latency, uptime, failures}}
            
        Returns:
            Fitness score (0.0-1.0), higher is better
        """
        
        # Get weights and apply task adjustments
        weights = self.weights.copy()
        if task_type in self.task_adjustments:
            for key, multiplier in self.task_adjustments[task_type].items():
                weights[key] *= multiplier
        
        # Normalize weights back to sum=1.0
        weight_sum = sum(weights.values())
        weights = {k: v / weight_sum for k, v in weights.items()}
        
        # Get baseline metrics
        elo = model.get('elo', 1200) / 1500.0  # Normalize to 1500 max
        cost = model.get('cost', 0.01)
        
        # Get real-time metrics if available
        metrics = metrics_data.get(model.get('model_id', ''), {}) if metrics_data else {}
        
        latency_ms = metrics.get('avg_latency_ms', self._estimate_latency(model))
        uptime = metrics.get('uptime_percent', 99.9)
        failures = metrics.get('recent_failures', 0)
        
        # Normalize individual scores (0.0-1.0)
        elo_score = min(elo, 1.0)
        
        latency_score = self._normalize_latency(latency_ms)  # Faster = higher
        
        cost_score = self._normalize_cost(cost, budget_constraint)  # Cheaper = higher
        
        reliability_score = (uptime / 100.0) * (1.0 - min(failures / 100.0, 0.5))
        
        availability_score = 1.0 if failures < 5 else 0.7 if failures < 10 else 0.4
        
        # Budget constraint adjustments
        if budget_constraint in ['free', 'cheap']:
            cost_score *= 1.5  # Strongly prefer cheaper
        elif budget_constraint == 'premium':
            cost_score *= 0.7  # Less concerned about cost
        
        # Calculate weighted fitness
        fitness = (
            weights['elo'] * elo_score +
            weights['latency'] * latency_score +
            weights['cost'] * cost_score +
            weights['reliability'] * reliability_score +
            weights['availability'] * availability_score
        )
        
        return min(fitness, 1.0)
    
    def _estimate_latency(self, model: Dict) -> float:
        """Estimate latency based on provider baseline"""
        provider = model.get('provider', '')
        
        # Latency baselines (ms) - from GRAEAE historical data
        baselines = {
            'groq': 1200,           # Ultra-fast
            'xai': 2000,            # Fast
            'perplexity': 5600,     # Fast
            'google': 3400,         # Medium
            'anthropic': 8600,      # Slower but high quality
            'openai': 8000,         # Medium-slow
            'together_ai': 4000,    # Medium
            'meta': 3000,           # Medium
            'local': 500,           # Very fast (local)
            'ollama': 300,          # Ultra-fast (local)
        }
        
        return baselines.get(provider, 5000)
    
    def _normalize_latency(self, latency_ms: float) -> float:
        """Normalize latency to 0.0-1.0 (higher latency = lower score)"""
        # Target: <1s is excellent (1.0), 10s is poor (0.0)
        # Use sigmoid-like curve
        if latency_ms <= 500:
            return 1.0
        elif latency_ms >= 10000:
            return 0.0
        else:
            # Linear interpolation between 500ms and 10s
            return 1.0 - (latency_ms - 500) / 9500.0
    
    def _normalize_cost(self, cost_per_request: float, budget: str) -> float:
        """Normalize cost based on budget constraint"""
        # Free tier: <$0.001 is excellent, >$0.01 is poor
        # Cheap tier: <$0.01 is excellent, >$0.05 is poor
        # Balanced tier: <$0.02 is excellent, >$0.10 is poor
        # Premium tier: cost doesn't matter much
        
        thresholds = {
            'free': (0.0, 0.001, 0.01),       # ideal, acceptable_max, unacceptable
            'cheap': (0.0, 0.01, 0.05),
            'balanced': (0.0, 0.02, 0.10),
            'premium': (0.0, 0.05, 0.20)
        }
        
        if budget not in thresholds:
            budget = 'balanced'
        
        ideal, acceptable_max, unacceptable = thresholds[budget]
        
        if cost_per_request <= ideal:
            return 1.0
        elif cost_per_request <= acceptable_max:
            ratio = (acceptable_max - cost_per_request) / (acceptable_max - ideal)
            return 0.8 + (0.2 * ratio)  # 0.8 - 1.0 range
        elif cost_per_request <= unacceptable:
            ratio = (unacceptable - cost_per_request) / (unacceptable - acceptable_max)
            return 0.2 + (0.6 * ratio)  # 0.2 - 0.8 range
        else:
            return 0.1  # Unacceptable but not zero
    
    def rank_models(self,
                   models: List[Dict],
                   task_type: str,
                   budget_constraint: str,
                   metrics_data: Dict = None) -> List[Tuple[Dict, float, str]]:
        """
        Rank models by fitness score
        
        Returns:
            List of (model, fitness_score, reasoning) tuples, sorted by fitness descending
        """
        ranked = []
        
        for model in models:
            fitness = self.calculate_fitness(
                model, task_type, budget_constraint, metrics_data
            )
            
            # Generate reasoning
            reasoning = self._generate_reasoning(
                model, fitness, task_type, budget_constraint
            )
            
            ranked.append((model, fitness, reasoning))
        
        # Sort by fitness descending
        ranked.sort(key=lambda x: x[1], reverse=True)
        
        return ranked
    
    def _generate_reasoning(self, model: Dict, fitness: float, 
                           task_type: str, budget: str) -> str:
        """Generate human-readable explanation for selection"""
        
        factors = []
        
        # Quality assessment
        elo = model.get('elo', 1200)
        if elo >= 1500:
            factors.append(f"Excellent quality (Elo {elo})")
        elif elo >= 1400:
            factors.append(f"Very good quality (Elo {elo})")
        else:
            factors.append(f"Good quality (Elo {elo})")
        
        # Speed assessment
        provider = model.get('provider', '')
        if provider in ['groq', 'ollama']:
            factors.append("Ultra-fast response")
        elif provider in ['xai', 'google']:
            factors.append("Fast response")
        else:
            factors.append("Standard latency")
        
        # Cost assessment
        cost = model.get('cost', 0)
        if cost == 0:
            factors.append("Free")
        elif cost < 0.005:
            factors.append("Very affordable")
        elif cost < 0.02:
            factors.append("Affordable")
        else:
            factors.append(f"Cost ${cost:.3f}")
        
        # Task fit
        if task_type in ['speed', 'fast_categorization']:
            factors.append(f"Well-suited for {task_type}")
        elif task_type in ['reasoning', 'research']:
            factors.append("Excellent for complex reasoning")
        elif task_type == 'coding':
            factors.append("Good for code generation")
        
        return f"Fitness {fitness:.2f}: {' • '.join(factors)}"

# Test it
if __name__ == '__main__':
    calc = FitnessCalculator()
    
    # Example models from Phase 1 optimized config
    models = [
        {'name': 'Groq Compound', 'provider': 'groq', 'elo': 1500, 'cost': 0.002, 'model_id': 'groq/compound'},
        {'name': 'Grok 4.1', 'provider': 'xai', 'elo': 1500, 'cost': 0.02, 'model_id': 'grok-4-1-fast-reasoning'},
        {'name': 'Claude Opus 4.5', 'provider': 'anthropic', 'elo': 1500, 'cost': 0.015, 'model_id': 'claude-opus-4-5-20251101'},
    ]
    
    # Test different task scenarios
    scenarios = [
        ('speed', 'cheap'),
        ('reasoning', 'premium'),
        ('coding', 'balanced'),
        ('fast_categorization', 'free'),
    ]
    
    for task_type, budget in scenarios:
        print(f"\n{task_type.upper()} + {budget.upper()} budget:")
        ranked = calc.rank_models(models, task_type, budget)
        for model, fitness, reasoning in ranked:
            print(f"  {model['name']:20} {fitness:.2f}  {reasoning}")
