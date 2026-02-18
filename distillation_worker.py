#!/usr/bin/env python3
"""
Background worker for LLM-based memory distillation
Runs continuously, processes unoptimized memories
"""

import asyncio
import logging
from datetime import datetime
import httpx
import json
import psycopg

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

class LLMDistillationService:
    def __init__(self, base_url: str = "http://localhost:11434"):
        self.base_url = base_url
        self.model = None
        self.client = httpx.AsyncClient()
        self.models_to_try = [
            "hf.co/microsoft/phi-3-mini-4k-instruct-gguf",
            "tinyllama",
            "mistral:latest",
        ]

    async def detect_best_model(self) -> str:
        """Detect best available model on this system"""
        if self.model:
            return self.model

        # Try to get available models
        try:
            response = await asyncio.wait_for(
                self.client.get(f"{self.base_url}/api/tags"),
                timeout=5.0
            )
            if response.status_code == 200:
                data = response.json()
                installed_models = [m["name"] for m in data.get("models", [])]
                logger.info(f"Available models: {installed_models}")

                # Find first available model from our preference list
                for model_name in self.models_to_try:
                    if any(model_name in m for m in installed_models):
                        self.model = model_name
                        logger.info(f"✅ Using model: {model_name}")
                        return model_name
        except Exception as e:
            logger.warning(f"Could not query Ollama: {e}")

        # Fallback: use first model in preference list
        self.model = self.models_to_try[0]
        logger.info(f"Using fallback model: {self.model}")
        return self.model

    async def distill(self, text: str, max_length: int = None) -> str:
        """Use LLM to intelligently compress text"""
        if not self.model:
            await self.detect_best_model()

        prompt = f"""Summarize this text to {max_length or 40}% of original length.
Keep critical information and context:

{text}

Summary (keep only essential):"""

        response = await self.client.post(
            f"{self.base_url}/api/generate",
            json={
                "model": self.model,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "temperature": 0.3,
                    "top_p": 0.9,
                }
            },
            timeout=30.0
        )

        result = response.json()
        return result.get("response", "").strip()

    async def assess_quality(self, original: str, compressed: str) -> float:
        """Use LLM to assess compression quality"""
        if not self.model:
            await self.detect_best_model()

        prompt = f"""Rate the quality of this compression (0-100).
Original: {original[:200]}...
Compressed: {compressed}

Quality score (0-100 only):"""

        response = await self.client.post(
            f"{self.base_url}/api/generate",
            json={
                "model": self.model,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.1}
            },
            timeout=45.0
        )

        result = response.json()
        try:
            score = int(result.get("response", "80").strip())
            return max(0, min(100, score))
        except:
            return 80


class MemoryDistillationWorker:
    def __init__(self):
        self.db = None
        self.llm = LLMDistillationService()
        self.check_interval = 30
        self.batch_size = 5

    async def start(self):
        """Start background worker"""
        # Connect as mnemos_user (the actual PostgreSQL role)
        self.db = await psycopg.AsyncConnection.connect(
            "postgresql://mnemos_user@localhost:5432/mnemos"
        )
        logger.info("Distillation worker started")

        while True:
            try:
                await self.process_batch()
            except Exception as e:
                logger.error(f"Worker error: {e}", exc_info=True)

            await asyncio.sleep(self.check_interval)

    async def process_batch(self):
        """Process a batch of memories"""
        query = """
        SELECT id, content, quality_rating
        FROM memories
        WHERE llm_optimized = false
          AND content IS NOT NULL
          AND LENGTH(content) > 100
        ORDER BY created DESC
        LIMIT %s
        """

        async with self.db.cursor() as cur:
            await cur.execute(query, (self.batch_size,))
            rows = await cur.fetchall()

        # Convert tuples to dicts for easier handling
        memories = []
        for row in rows:
            memories.append({
                'id': row[0],
                'content': row[1],
                'quality_rating': row[2] or 75
            })

        if not memories:
            logger.debug("No memories to optimize")
            return

        logger.info(f"Processing {len(memories)} memories")

        for memory in memories:
            try:
                await self.optimize_memory(memory)
            except Exception as e:
                logger.error(f"Error optimizing {memory['id']}: {e}")

    async def optimize_memory(self, memory):
        """Optimize single memory with LLM"""
        memory_id = memory["id"]
        original_text = memory["content"]
        current_quality = memory["quality_rating"]

        try:
            # Generate better compression via LLM
            llm_compression = await asyncio.wait_for(
                self.llm.distill(
                    original_text,
                    max_length=int(len(original_text) * 0.4)
                ),
                timeout=45.0
            )

            # Assess quality
            quality_score = await asyncio.wait_for(
                self.llm.assess_quality(original_text, llm_compression),
                timeout=30.0
            )

            # Update database
            update_query = """
            UPDATE memories
            SET compressed_content = %s,
                quality_rating = %s,
                llm_optimized = true,
                optimized_at = NOW()
            WHERE id = %s
            """

            async with self.db.cursor() as cur:
                await cur.execute(
                    update_query,
                    (llm_compression, min(100, int(quality_score)), memory_id)
                )

            logger.info(
                f"Optimized {memory_id}: "
                f"quality {current_quality} → {quality_score:.0f}"
            )

        except asyncio.TimeoutError:
            logger.warning(f"Timeout optimizing {memory_id}")
        except Exception as e:
            logger.error(f"Error optimizing {memory_id}: {e}")


async def main():
    worker = MemoryDistillationWorker()
    try:
        await worker.start()
    except KeyboardInterrupt:
        logger.info("Worker shutting down")


if __name__ == "__main__":
    asyncio.run(main())
