# ARCHIVED — extracted from pre-refactor history (2026-04-12)
# NOT wired into production. Review README.md in this directory before integrating.
# Source: see /opt/mnemos/archive/README.md

#!/usr/bin/env python3
"""
GraeaeResponseRanker - Display independent LM Arena rankings with each provider response.
Like Yelp/Google reviews: shows actual content + independent quality ratings.

This layer sits between Graeae and the caller to display:
1. Each provider's response with its Elo ranking
2. Quality metrics (Elo score, latency, cost, reliability)
3. Category rankings ("Best for reasoning", "Best for speed", etc.)
4. Visual rating display (stars based on Elo percentile)
"""

import json
import os
from typing import Dict, List, Any
from datetime import datetime

_here = os.path.dirname(os.path.abspath(__file__))
_default_ranking_path = os.path.join(_here, "provider_ranking.json")


class GraeaeResponseRanker:
    def __init__(self, provider_ranking_path: str = _default_ranking_path):
        """Initialize with provider rankings"""
        self.provider_rankings = self._load_rankings(provider_ranking_path)
        self.provider_map = self._build_provider_map()
        self.category_rankings = self.provider_rankings.get("rankings_by_category", {})

    def _load_rankings(self, path: str) -> Dict:
        """Load provider ranking data"""
        try:
            with open(path, 'r') as f:
                return json.load(f)
        except Exception:
            return {"providers": [], "rankings_by_category": {}}

    def _build_provider_map(self) -> Dict[str, Dict]:
        """Build map of provider ID to provider details"""
        provider_map = {}
        for provider in self.provider_rankings.get("providers", []):
            provider_map[provider["id"]] = provider
            provider_map[provider["provider"]] = provider  # Also map by provider name
        return provider_map

    def get_provider_ranking(self, provider_id: str) -> Dict[str, Any]:
        """Get ranking details for a specific provider"""
        if provider_id in self.provider_map:
            provider = self.provider_map[provider_id]
            return {
                "id": provider.get("id"),
                "name": provider.get("name"),
                "elo_score": provider.get("elo_score", 0),
                "tier": provider.get("tier", "unknown"),
                "category": provider.get("category", "general"),
                "quality_score": provider.get("quality_score", 0),
                "reliability": provider.get("reliability", 0),
                "cost_per_1m": provider.get("cost_per_1m_tokens", 0),
                "speed_ms": provider.get("speed_ms", 0),
                "capabilities": provider.get("capabilities", []),
                "context_window": provider.get("context_window", 0),
            }
        return {}

    def get_elo_stars(self, elo_score: int) -> str:
        """Convert Elo score to star rating (1-5 stars)"""
        # Scale: Elo 1000-1300 maps to 1-5 stars
        if elo_score >= 1270:
            return "*****"  # 5 stars - frontier
        elif elo_score >= 1250:
            return "****"   # 4 stars - strong
        elif elo_score >= 1220:
            return "***"    # 3 stars - competitive
        elif elo_score >= 1100:
            return "**"     # 2 stars - value
        else:
            return "*"      # 1 star - local

    def format_provider_rating(self, provider_id: str, quality_score: float = None,
                               latency_ms: int = None, cost: float = None) -> str:
        """Format a review-style rating card for a provider"""
        ranking = self.get_provider_ranking(provider_id)

        if not ranking:
            return f"? {provider_id}: No ranking data"

        # Use provided metrics or defaults from ranking
        quality = quality_score if quality_score is not None else ranking.get("quality_score", 0)
        latency = latency_ms if latency_ms is not None else ranking.get("speed_ms", 0)
        cost_val = cost if cost is not None else ranking.get("cost_per_1m", 0)
        elo = ranking.get("elo_score", 0)

        stars = self.get_elo_stars(elo)
        name = ranking.get("name", provider_id)
        tier = ranking.get("tier", "").title()

        # Format like review card with right-margin borders
        card = (
            f"\n"
            f"╔════════════════════════════════════════════════════════════════╗\n"
            f"║ {stars} {name:40s} [{tier}] ║\n"
            f"║ Elo Score: {elo:4d} | Quality: {quality:5.0%} | Reliability: {ranking.get('reliability', 0):5.0%} ║\n"
            f"║ Speed: {latency:4d}ms | Cost: ${cost_val:6.2f}/1M tokens | Context: {ranking.get('context_window', 0):,d} ║\n"
            f"║ Category: {ranking.get('category', 'general').title():15s} | Capabilities: {', '.join(ranking.get('capabilities', [])[:3])} ║\n"
            f"╚════════════════════════════════════════════════════════════════╝\n"
        )
        return card

    def format_consensus_with_rankings(self, response: Dict[str, Any],
                                       detailed: bool = True) -> str:
        """Format Graeae consensus response with all provider rankings displayed"""
        output = []

        # Header
        output.append("\n" + "=" * 70)
        output.append(f"GRAEAE CONSENSUS - {response.get('consensus_score', 0):.0%} Agreement")
        output.append("=" * 70)

        # Determine winning provider: use 'best_provider' key if present,
        # otherwise derive from all_responses by highest final_score
        all_responses = response.get("all_responses", {})
        winning_provider = response.get("best_provider", "unknown")
        if winning_provider == "unknown" and all_responses:
            winning_provider = max(
                all_responses.items(),
                key=lambda kv: kv[1].get("final_score", 0.0) if isinstance(kv[1], dict) else 0.0
            )[0]

        winning_response = ""
        if winning_provider in all_responses and isinstance(all_responses[winning_provider], dict):
            winning_response = all_responses[winning_provider].get("response_text", "")

        output.append(f"\nWINNER: {winning_provider.upper()}")
        output.append(self.format_provider_rating(winning_provider))
        if len(winning_response) > 300:
            output.append(f"\n{winning_response[:300]}...")
        else:
            output.append(f"\n{winning_response}")

        # ALL PROVIDER RANKINGS (like reviews)
        if detailed and all_responses:
            output.append("\n" + "-" * 70)
            output.append("ALL PROVIDER RANKINGS")
            output.append("-" * 70)

            all_providers = all_responses
            sorted_providers = sorted(
                all_providers.items(),
                key=lambda x: x[1].get("final_score", 0.0) if isinstance(x[1], dict) else 0.0,
                reverse=True
            )

            for provider_name, provider_response in sorted_providers[:9]:
                if isinstance(provider_response, dict):
                    output.append(self.format_provider_rating(provider_name))
                    preview = provider_response.get("response_text", "")[:150]
                    if preview:
                        output.append(f"  {preview}...")

        # Summary metrics
        output.append("\n" + "-" * 70)
        output.append("CONSENSUS METRICS")
        output.append("-" * 70)
        output.append(f"  Consensus Score: {response.get('consensus_score', 0):.0%}")
        output.append(f"  Avg Elo Across Providers: {self._avg_elo(response):.0f}")
        output.append(f"  Providers Queried: {len(all_responses)}")
        output.append(f"  Total Cost: ${response.get('cost', 0):.4f}")
        output.append(f"  Fastest Response: {self._fastest_latency(response)}ms")

        output.append("\n" + "=" * 70 + "\n")

        return "\n".join(output)

    def _avg_elo(self, response: Dict) -> float:
        """Calculate average Elo score across all providers"""
        elos = []
        for provider_id in response.get("all_responses", {}).keys():
            ranking = self.get_provider_ranking(provider_id)
            if ranking:
                elos.append(ranking.get("elo_score", 0))
        return sum(elos) / len(elos) if elos else 0

    def _fastest_latency(self, response: Dict) -> int:
        """Find fastest latency across all responses"""
        latencies = []
        for provider_response in response.get("all_responses", {}).values():
            if isinstance(provider_response, dict) and "latency_ms" in provider_response:
                latencies.append(provider_response["latency_ms"])
        return min(latencies) if latencies else 0

    def rank_providers_by_category(self, category: str = "reasoning") -> List[Dict]:
        """Get providers ranked for a specific category"""
        provider_ids = self.category_rankings.get(category, [])
        ranked = []

        for provider_id in provider_ids:
            ranking = self.get_provider_ranking(provider_id)
            if ranking:
                ranked.append({
                    "rank": len(ranked) + 1,
                    "name": ranking.get("name"),
                    "elo": ranking.get("elo_score"),
                    "stars": self.get_elo_stars(ranking.get("elo_score", 0)),
                    "quality": ranking.get("quality_score"),
                    "id": provider_id
                })

        return ranked

    def format_category_rankings(self, category: str = "reasoning") -> str:
        """Format rankings by category (like app store 'Best for' sections)"""
        rankings = self.rank_providers_by_category(category)

        output = [f"\nBest for {category.upper()}"]
        output.append("-" * 50)

        for item in rankings[:5]:  # Top 5
            output.append(
                f"  #{item['rank']} {item['stars']} {item['name']:30s} (Elo: {item['elo']})"
            )

        return "\n".join(output)


# Example usage and CLI interface
if __name__ == "__main__":
    import sys

    ranker = GraeaeResponseRanker()

    if len(sys.argv) > 1 and sys.argv[1] == "test":
        # Test mode: show sample response with rankings
        print("\nLM ARENA RANKING DISPLAY EXAMPLES")
        print("=" * 70)

        # Show category rankings
        for category in ["reasoning", "coding", "speed", "research", "cost"]:
            print(ranker.format_category_rankings(category))

        # Show provider ratings
        print("\n\n" + "=" * 70)
        print("Individual Provider Ratings")
        print("=" * 70)
        for provider in ["openai", "gemini", "groq", "nvidia"]:
            print(ranker.format_provider_rating(provider))
