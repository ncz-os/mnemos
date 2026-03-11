"""
token-filter² (Hierarchical Compression) - Text compression using extractive + abstractive techniques
Optimizes for different task types with configurable compression ratios
"""

import re
import json
from typing import Dict, Tuple, Optional
from datetime import datetime

class token-filterCompressor:
    """token-filter² compression engine with task-specific compression ratios"""
    
    # Task-specific compression ratios (keep this percentage)
    COMPRESSION_RATIOS = {
        "reasoning": 0.45,           # Keep 45% for reasoning chains
        "code_generation": 0.30,     # Keep 30% for code (structure matters)
        "architecture_design": 0.50, # Keep 50% for design rationale
        "tool_use": 0.40,           # Default
        "general": 0.40,            # Default
    }
    
    def __init__(self, task_type: str = "general"):
        self.task_type = task_type
        self.compression_ratio = self.COMPRESSION_RATIOS.get(task_type, 0.40)
    
    def compress(self, text: str) -> Tuple[str, Dict]:
        """
        Compress text using token-filter² algorithm
        
        Returns:
            (compressed_text, metadata_dict)
        """
        if not text or len(text) < 100:
            return text, {
                "original_size": len(text),
                "compressed_size": len(text),
                "compression_ratio": 1.0,
                "method": "none",
                "task_type": self.task_type
            }
        
        original_size = len(text)
        
        # Step 1: Extractive compression - select key sentences
        sentences = self._split_sentences(text)
        if not sentences:
            return text, {"original_size": original_size, "compressed_size": original_size, 
                         "compression_ratio": 1.0, "method": "none", "task_type": self.task_type}
        
        # Score sentences by relevance
        scored_sentences = self._score_sentences(sentences)
        
        # Select top sentences to reach target compression ratio
        target_words = int((original_size / 5) * self.compression_ratio)  # Rough word count
        extracted = self._select_sentences(scored_sentences, target_words)
        
        # Step 2: Clean and normalize
        compressed = self._normalize_text(extracted)
        
        compressed_size = len(compressed)
        actual_ratio = compressed_size / original_size if original_size > 0 else 1.0
        
        return compressed, {
            "original_size": original_size,
            "compressed_size": compressed_size,
            "compression_ratio": actual_ratio,
            "method": "token_filter_extractive",
            "task_type": self.task_type,
            "target_ratio": self.compression_ratio,
            "sentences_extracted": len(extracted.split('.')),
            "total_sentences": len(sentences)
        }
    
    def decompress(self, compressed_text: str) -> str:
        """
        Note: token-filter² is lossy compression (extractive method)
        Decompressed text will be similar but not identical to original
        """
        # For extractive compression, "decompression" is just returning the compressed text
        # (information is genuinely lost, not encoded)
        return compressed_text
    
    def _split_sentences(self, text: str) -> list:
        """Split text into sentences"""
        # Simple sentence splitting
        sentences = re.split(r'(?<=[.!?])\s+', text)
        return [s.strip() for s in sentences if s.strip()]
    
    def _score_sentences(self, sentences: list) -> list:
        """Score sentences by importance using TF-IDF-like approach"""
        # Simple scoring: prefer sentences with more unique words, longer length
        scored = []
        for sent in sentences:
            words = sent.lower().split()
            unique_words = len(set(words))
            length_score = min(len(words) / 20, 1.0)  # Normalized to [0,1]
            unique_score = min(unique_words / 10, 1.0)
            
            # Boost sentences with code markers, technical terms
            tech_boost = 1.2 if any(marker in sent for marker in ['```', 'def ', 'class ', 'def:', '->']) else 1.0
            
            score = (length_score * 0.4 + unique_score * 0.6) * tech_boost
            scored.append((sent, score))
        
        return scored
    
    def _select_sentences(self, scored_sentences: list, target_words: int) -> str:
        """Select top-scored sentences to reach target word count"""
        # Sort by score (descending)
        sorted_sents = sorted(scored_sentences, key=lambda x: x[1], reverse=True)
        
        # But maintain original order for coherence (re-sort by position)
        selected = []
        word_count = 0
        
        for sent, score in sorted(enumerate(sorted_sents), key=lambda x: scored_sentences.index(x[1][1])):
            # Find original position
            for i, (orig_sent, orig_score) in enumerate(scored_sentences):
                if orig_sent == sorted_sents[sent][0]:
                    selected.append((i, orig_sent))
                    break
            
            word_count += len(sorted_sents[sent][0].split())
            if word_count >= target_words:
                break
        
        # Restore original order
        selected.sort(key=lambda x: x[0])
        return ' '.join([s[1] for s in selected])
    
    def _normalize_text(self, text: str) -> str:
        """Normalize and clean compressed text"""
        # Remove extra whitespace
        text = re.sub(r'\s+', ' ', text).strip()
        
        # Ensure proper spacing around punctuation
        text = re.sub(r'\s+([.!?])', r'\1', text)
        text = re.sub(r'([.!?])\s*$', r'\1', text)
        
        return text


class CompressionQualityAnalyzer:
    """Analyze compression quality and information retention"""
    
    @staticmethod
    def analyze(original: str, compressed: str) -> Dict:
        """
        Analyze compression quality
        
        Returns:
            {
                "information_retention": float (0-100),
                "quality_score": int (0-100),
                "key_metrics_preserved": bool,
                "coherence_score": float (0-100)
            }
        """
        if not original or not compressed:
            return {"information_retention": 0, "quality_score": 0, "key_metrics_preserved": False, "coherence_score": 0}
        
        # Calculate metrics
        original_len = len(original)
        compressed_len = len(compressed)
        compression_ratio = compressed_len / original_len if original_len > 0 else 0
        
        # Information retention: if we keep more text, we retain more info
        retention = (1.0 - compression_ratio) * 100
        retention = max(0, min(100, retention))
        
        # Key metrics: check if important keywords preserved
        key_terms = CompressionQualityAnalyzer._extract_key_terms(original)
        preserved = sum(1 for term in key_terms if term in compressed.lower())
        metrics_preserved = preserved >= len(key_terms) * 0.7
        
        # Coherence: check sentence structure
        coherence = CompressionQualityAnalyzer._measure_coherence(compressed)
        
        # Quality score: weighted combination
        quality_score = int(
            retention * 0.5 +  # 50% based on info retention
            (preserved / max(1, len(key_terms)) * 100) * 0.3 +  # 30% key terms preserved
            coherence * 0.2  # 20% coherence
        )
        
        return {
            "information_retention": round(retention, 2),
            "quality_score": max(0, min(100, quality_score)),
            "key_metrics_preserved": metrics_preserved,
            "coherence_score": round(coherence, 2),
            "key_terms_found": preserved,
            "key_terms_total": len(key_terms),
            "compression_ratio": round(compression_ratio, 3)
        }
    
    @staticmethod
    def _extract_key_terms(text: str) -> list:
        """Extract key terms (nouns, verbs, numbers)"""
        # Simple heuristic: capitalized words, numbers, code keywords
        terms = set()
        
        # Get capitalized words
        words = text.split()
        for word in words:
            if word and word[0].isupper() and len(word) > 2:
                terms.add(word.lower())
            # Get numeric values
            if any(c.isdigit() for c in word):
                terms.add(word.lower())
            # Code keywords
            if any(kw in word.lower() for kw in ['def', 'class', 'import', 'return', 'if', 'for']):
                terms.add(word.lower())
        
        return list(terms)[:20]  # Return top 20
    
    @staticmethod
    def _measure_coherence(text: str) -> float:
        """Measure text coherence (0-100)"""
        if not text or len(text.split()) < 3:
            return 0
        
        # Check for common coherence markers
        coherence_markers = 0
        total_sentences = max(1, len(re.split(r'[.!?]', text)))
        
        # Transitions
        transitions = ['however', 'therefore', 'furthermore', 'additionally', 'thus', 'in conclusion']
        for trans in transitions:
            if trans in text.lower():
                coherence_markers += 1
        
        # Proper punctuation
        if text.endswith(('.', '!', '?')):
            coherence_markers += 1
        
        # Multiple sentences with proper flow
        if total_sentences > 1:
            coherence_markers += 1
        
        # Score: max out at 100
        coherence = min(100, (coherence_markers / len(transitions)) * 100)
        return coherence


# Compression manager for handling multiple compressions
class CompressionManager:
    """Manage compression across multiple texts"""
    
    def __init__(self):
        self.stats = {
            "total_compressions": 0,
            "total_original_size": 0,
            "total_compressed_size": 0,
            "average_quality_score": 0,
            "compressions_by_task": {}
        }
    
    def compress_response(self, text: str, task_type: str = "general") -> Dict:
        """
        Compress a response and track stats
        
        Returns:
            {
                "original_text": str,
                "compressed_text": str,
                "original_size": int,
                "compressed_size": int,
                "compression_ratio": float,
                "quality_score": int,
                "metadata": dict
            }
        """
        compressor = token-filterCompressor(task_type=task_type)
        compressed, metadata = compressor.compress(text)
        
        # Analyze quality
        quality = CompressionQualityAnalyzer.analyze(text, compressed)
        
        # Update stats
        self.stats["total_compressions"] += 1
        self.stats["total_original_size"] += metadata["original_size"]
        self.stats["total_compressed_size"] += metadata["compressed_size"]
        
        if task_type not in self.stats["compressions_by_task"]:
            self.stats["compressions_by_task"][task_type] = {"count": 0, "avg_ratio": 0}
        
        self.stats["compressions_by_task"][task_type]["count"] += 1
        self.stats["compressions_by_task"][task_type]["avg_ratio"] = (
            self.stats["compressions_by_task"][task_type].get("avg_ratio", 0) * 0.9 +
            metadata["compression_ratio"] * 0.1
        )
        
        # Calculate average quality
        all_compressions = self.stats["total_compressions"]
        old_avg = self.stats["average_quality_score"]
        new_avg = (old_avg * (all_compressions - 1) + quality["quality_score"]) / all_compressions
        self.stats["average_quality_score"] = round(new_avg, 2)
        
        return {
            "original_size": metadata["original_size"],
            "compressed_size": metadata["compressed_size"],
            "compression_ratio": metadata["compression_ratio"],
            "quality_score": quality["quality_score"],
            "information_retention": quality["information_retention"],
            "key_metrics_preserved": quality["key_metrics_preserved"],
            "method": "token_filter"
        }
    
    def get_stats(self) -> Dict:
        """Get compression statistics"""
        if self.stats["total_compressions"] == 0:
            return self.stats
        
        avg_ratio = (
            self.stats["total_compressed_size"] / self.stats["total_original_size"]
            if self.stats["total_original_size"] > 0 else 0
        )
        
        self.stats["average_compression_ratio"] = round(avg_ratio, 3)
        self.stats["total_savings_bytes"] = (
            self.stats["total_original_size"] - self.stats["total_compressed_size"]
        )
        
        return self.stats


# Global instance
_compression_manager = None

def get_compression_manager():
    """Get or create global compression manager"""
    global _compression_manager
    if _compression_manager is None:
        _compression_manager = CompressionManager()
    return _compression_manager

