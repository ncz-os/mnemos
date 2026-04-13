# ARCHIVED — extracted from pre-refactor history (2026-04-12)
# NOT wired into production. Review README.md in this directory before integrating.
# Source: see /opt/mnemos/archive/README.md

#!/usr/bin/env python3
"""
Graeae Response Ranker - Display independent LM Arena rankings with each muse response
Like Yelp/Google reviews: shows actual content + independent quality ratings

This layer sits between Graeae and Claude to display:
1. Each muse's response with its Elo ranking
2. Quality metrics (Elo score, latency, cost, reliability)
3. Category rankings ("Best for reasoning", "Best for speed", etc.)
4. Visual rating display (⭐ stars based on Elo percentile)
"""

import json
import requests
from typing import Dict, List, Any
from datetime import datetime

class GraeaeResponseRanker:
    def __init__(self, provider_ranking_path: str = "provider_ranking.json"):
        """Initialize with provider rankings"""
        self.provider_rankings = self._load_rankings(provider_ranking_path)
        self.provider_map = self._build_provider_map()
        self.category_rankings = self.provider_rankings.get("rankings_by_category", {})

    def _load_rankings(self, path: str) -> Dict:
        """Load provider ranking data"""
        try:
            with open(path, 'r') as f:
                return json.load(f)
        except:
            return {"providers": [], "rankings_by_category": {}}

    def _build_provider_map(self) -> Dict[str, Dict]:
        """Build map of provider ID to provider details"""
        provider_map = {}
        for provider in self.provider_rankings.get("providers", []):
            provider_map[provider["id"]] = provider
            provider_map[provider["provider"]] = provider  # Also map by provider name
        return provider_map

    def get_provider_ranking(self, muse_id: str) -> Dict[str, Any]:
        """Get ranking details for a specific muse"""
        if muse_id in self.provider_map:
            provider = self.provider_map[muse_id]
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
            return "⭐⭐⭐⭐⭐"  # 5 stars - frontier
        elif elo_score >= 1250:
            return "⭐⭐⭐⭐"    # 4 stars - strong
        elif elo_score >= 1220:
            return "⭐⭐⭐"      # 3 stars - competitive
        elif elo_score >= 1100:
            return "⭐⭐"        # 2 stars - value
        else:
            return "⭐"         # 1 star - local

    def format_muse_rating(self, muse_id: str, quality_score: float = None,
                          latency_ms: int = None, cost: float = None) -> str:
        """Format a Yelp/Google-style rating card for a muse"""
        ranking = self.get_provider_ranking(muse_id)

        if not ranking:
            return f"❓ {muse_id}: No ranking data"

        # Use provided metrics or defaults from ranking
        quality = quality_score or ranking.get("quality_score", 0)
        latency = latency_ms or ranking.get("speed_ms", 0)
        cost = cost or ranking.get("cost_per_1m", 0)
        elo = ranking.get("elo_score", 0)

        stars = self.get_elo_stars(elo)
        name = ranking.get("name", muse_id)
        tier = ranking.get("tier", "").title()

        # Format like review card
        card = f"""
╔════════════════════════════════════════════════════════════════╗
║ {stars} {name:40s} [{tier}]
║ Elo Score: {elo:4d} | Quality: {quality:5.0%} | Reliability: {ranking.get('reliability', 0):5.0%}
║ Speed: {latency:4d}ms | Cost: ${cost:6.2f}/1M tokens | Context: {ranking.get('context_window', 0):,d}
║ Category: {ranking.get('category', 'general').title():15s} | Capabilities: {', '.join(ranking.get('capabilities', [])[:3])}
╚════════════════════════════════════════════════════════════════╝
"""
        return card

    def format_consensus_with_rankings(self, response: Dict[str, Any],
                                       detailed: bool = True) -> str:
        """Format Graeae consensus response with all muse rankings displayed"""
        output = []

        # Header
        output.append("\n" + "="*70)
        output.append(f"🏆 GRAEAE CONSENSUS - {response.get('consensus_score', 0):.0%} Agreement")
        output.append("="*70)

        # Winning muse prominently displayed
        winning_muse = response.get("winning_muse", "unknown")
        winning_response = response.get("winning_response", "")

        output.append(f"\n✨ WINNER: {winning_muse.upper()}")
        output.append(self.format_muse_rating(winning_muse))
        output.append(f"\n{winning_response[:300]}..." if len(winning_response) > 300 else f"\n{winning_response}")

        # All muse rankings (like Yelp reviews)
        if detailed and "all_responses" in response:
            output.append("\n" + "─"*70)
            output.append("📋 ALL MUSE RANKINGS")
            output.append("─"*70)

            all_muses = response.get("all_responses", {})
            sorted_muses = sorted(
                all_muses.items(),
                key=lambda x: x[1].get("elo_score", 0) if isinstance(x[1], dict) else 0,
                reverse=True
            )

            for muse_id, muse_response in sorted_muses[:9]:  # Show all 9 muses
                if isinstance(muse_response, dict):
                    output.append(self.format_muse_rating(muse_id))
                    preview = muse_response.get("response", "")[:150]
                    if preview:
                        output.append(f"  📝 {preview}...")

        # Summary metrics
        output.append("\n" + "─"*70)
        output.append("📊 CONSENSUS METRICS")
        output.append("─"*70)
        output.append(f"  Consensus Score: {response.get('consensus_score', 0):.0%}")
        output.append(f"  Avg Elo Across Muses: {self._avg_elo(response):.0f}")
        output.append(f"  Muses Queried: {len(response.get('all_responses', {}))}")
        output.append(f"  Total Cost: ${response.get('cost', 0):.4f}")
        output.append(f"  Fastest Response: {self._fastest_latency(response)}ms")

        output.append("\n" + "="*70 + "\n")

        return "\n".join(output)

    def _avg_elo(self, response: Dict) -> float:
        """Calculate average Elo score across all muses"""
        elos = []
        for muse_id in response.get("all_responses", {}).keys():
            ranking = self.get_provider_ranking(muse_id)
            if ranking:
                elos.append(ranking.get("elo_score", 0))
        return sum(elos) / len(elos) if elos else 0

    def _fastest_latency(self, response: Dict) -> int:
        """Find fastest latency across all responses"""
        latencies = []
        for muse_response in response.get("all_responses", {}).values():
            if isinstance(muse_response, dict) and "latency_ms" in muse_response:
                latencies.append(muse_response["latency_ms"])
        return min(latencies) if latencies else 0

    def rank_muses_by_category(self, category: str = "reasoning") -> List[Dict]:
        """Get muses ranked for a specific category"""
        muse_ids = self.category_rankings.get(category, [])
        ranked = []

        for muse_id in muse_ids:
            ranking = self.get_provider_ranking(muse_id)
            if ranking:
                ranked.append({
                    "rank": len(ranked) + 1,
                    "name": ranking.get("name"),
                    "elo": ranking.get("elo_score"),
                    "stars": self.get_elo_stars(ranking.get("elo_score", 0)),
                    "quality": ranking.get("quality_score"),
                    "id": muse_id
                })

        return ranked

    def format_category_rankings(self, category: str = "reasoning") -> str:
        """Format rankings by category (like app store "Best for" sections)"""
        rankings = self.rank_muses_by_category(category)

        output = [f"\n🏅 Best for {category.upper()}"]
        output.append("─" * 50)

        for item in rankings[:5]:  # Top 5
            output.append(
                f"  #{item['rank']} {item['stars']} {item['name']:30s} (Elo: {item['elo']})"
            )

        return "\n".join(output)


# Example usage and CLI interface
if __name__ == "__main__":
    import sys
    import json

    ranker = GraeaeResponseRanker("provider_ranking.json")

    if len(sys.argv) > 1 and sys.argv[1] == "test":
        # Test mode: show sample response with rankings
        print("\n📊 LM ARENA RANKING DISPLAY EXAMPLES")
        print("="*70)

        # Show category rankings
        for category in ["reasoning", "coding", "speed", "research", "cost"]:
            print(ranker.format_category_rankings(category))

        # Show muse ratings
        print("\n\n" + "="*70)
        print("Individual Muse Ratings (like Google reviews)")
        print("="*70)
        for muse in ["gpt-5", "gemini-3", "groq", "vllm"]:
            print(ranker.format_muse_rating(muse))
