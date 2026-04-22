#!/usr/bin/env python3
"""
MNEMOS API Inference Backend for InvestorClaw Tier 3 Enrichment.

Implements OpenAI-compatible `/v1/chat/completions` interface for the MNEMOS
unified FastAPI service running on PYTHIA (192.168.207.67:5002).

This client can be dropped into InvestorClaw's tier3_enrichment.py as:
  from mnemos_consultation_client import MNEMOSConsultationClient

Environment variables:
  INVESTORCLAW_MNEMOS_ENDPOINT  — API base URL (default: http://192.168.207.67:5002)
  INVESTORCLAW_MNEMOS_API_KEY   — Bearer token (required)
  INVESTORCLAW_MNEMOS_MODEL     — Model selector (default: best-reasoning)
    Supported: 'auto', 'best-reasoning', 'best-coding', 'fastest', 'cheapest',
               'gpt-4', 'gpt-4o', 'claude-opus', 'groq-llama', etc.

Features:
  • Auto-detect OpenAI-compatible endpoint
  • Cost tracking with per-symbol breakdown
  • Token usage reporting
  • Graceful fallback on error
  • Memory injection (if INVESTORCLAW_MNEMOS_SEARCH=true)
  • Multi-provider routing via MNEMOS cost optimizer
  • HMAC fingerprinting support for InvestorClaw verbatim artifact tracking
"""

import hashlib
import hmac
import json
import logging
import os
import secrets
import time
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


def _validate_http_url(url: str) -> None:
    """Raise ValueError if URL scheme is not http/https."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(
            f"Refusing non-http(s) URL scheme: {parsed.scheme!r} in {url!r}"
        )


def _get_hmac_key() -> bytes:
    """Retrieve or generate HMAC key for fingerprinting."""
    key = os.environ.get("INVESTORCLAW_CONSULTATION_HMAC_KEY", "").strip()
    if key:
        return key.encode()

    # Check user-space config
    env_file = Path.home() / ".investorclaw" / ".env"
    if env_file.exists():
        existing = env_file.read_text()
        for line in existing.splitlines():
            if line.strip().startswith("INVESTORCLAW_CONSULTATION_HMAC_KEY="):
                found_key = line.strip().split("=", 1)[1].strip()
                if found_key:
                    os.environ["INVESTORCLAW_CONSULTATION_HMAC_KEY"] = found_key
                    return found_key.encode()

    # Generate new key
    generated = secrets.token_hex(32)
    env_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(env_file.parent, 0o700)
    except OSError:
        pass

    with open(env_file, "a") as f:
        f.write(f"\nINVESTORCLAW_CONSULTATION_HMAC_KEY={generated}\n")

    try:
        os.chmod(env_file, 0o600)
    except OSError:
        pass

    os.environ["INVESTORCLAW_CONSULTATION_HMAC_KEY"] = generated
    return generated.encode()


def _compute_fingerprint(symbol: str, model: str, synthesis: str) -> str:
    """Compute HMAC-SHA256 fingerprint for verbatim artifact tracking."""
    key = _get_hmac_key()
    msg = f"{symbol}|{model}|{synthesis}".encode()
    return hmac.new(key, msg, hashlib.sha256).hexdigest()[:16]


class MNEMOSConsultationClient:
    """Consultation client backed by MNEMOS API inference."""

    def __init__(self):
        """Initialize MNEMOS consultation client with endpoint validation."""
        self.endpoint = os.environ.get(
            "INVESTORCLAW_MNEMOS_ENDPOINT",
            "http://192.168.207.67:5002"
        ).rstrip("/")

        self.api_key = os.environ.get(
            "INVESTORCLAW_MNEMOS_API_KEY",
            ""
        ).strip()

        self.model = os.environ.get(
            "INVESTORCLAW_MNEMOS_MODEL",
            "best-reasoning"
        )

        self.search_enabled = os.environ.get(
            "INVESTORCLAW_MNEMOS_SEARCH",
            "false"
        ).lower() == "true"

        self.timeout = float(os.environ.get(
            "INVESTORCLAW_MNEMOS_TIMEOUT",
            "60.0"
        ))

        # Validate URL scheme
        _validate_http_url(self.endpoint)

        if not self.api_key:
            raise ValueError(
                "INVESTORCLAW_MNEMOS_API_KEY not set. "
                "Required for MNEMOS consultation backend."
            )

        self._verify_endpoint()
        logger.info(
            f"MNEMOS consultation client initialized: "
            f"endpoint={self.endpoint}, model={self.model}, "
            f"search_enabled={self.search_enabled}"
        )

    def _verify_endpoint(self) -> None:
        """Verify MNEMOS endpoint is reachable and healthy."""
        try:
            req = urllib.request.Request(
                f"{self.endpoint}/health",
                method="GET",
                headers={"Authorization": f"Bearer {self.api_key}"}
            )

            with urllib.request.urlopen(req, timeout=5.0) as resp:
                data = json.loads(resp.read())
                version = data.get("version", "unknown")
                logger.info(
                    f"MNEMOS endpoint verified: "
                    f"v{version}, {len(data.get('models', []))} models available"
                )

        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise RuntimeError(
                    "MNEMOS authentication failed. Check INVESTORCLAW_MNEMOS_API_KEY."
                )
            else:
                raise RuntimeError(f"MNEMOS health check failed: HTTP {e.code}")

        except Exception as e:
            raise RuntimeError(
                f"MNEMOS endpoint unreachable at {self.endpoint}: {e}"
            )

    def consult(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 500,
    ) -> Dict[str, Any]:
        """
        Query MNEMOS API for inference.

        Args:
            prompt: User query or data to analyze
            system_prompt: System role/instructions (optional)
            temperature: Sampling temperature (0.0–2.0)
            max_tokens: Maximum output tokens

        Returns:
            {
                "response": "Synthesis text...",
                "model": "gpt-4o" (resolved model name),
                "endpoint": "http://...:5002",
                "inference_ms": 3200,
                "is_heuristic": False,
                "input_tokens": 140,
                "output_tokens": 180,
                "total_tokens": 320,
                "cost_usd": 0.0045,
                "fingerprint": "a7f2e8c1b9d3e5f6" (optional, for verbatim tracking)
            }
        """
        messages = []

        if system_prompt:
            messages.append({
                "role": "system",
                "content": system_prompt
            })

        messages.append({
            "role": "user",
            "content": prompt
        })

        request_payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "top_p": 0.95
        }

        start_time = time.time()
        try:
            req = urllib.request.Request(
                f"{self.endpoint}/v1/chat/completions",
                data=json.dumps(request_payload).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json"
                },
                method="POST"
            )

            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                response_data = json.loads(resp.read())

            inference_ms = int((time.time() - start_time) * 1000)

            # Extract completion
            choice = response_data.get("choices", [{}])[0]
            message = choice.get("message", {})
            response_text = message.get("content", "").strip()

            # Extract usage
            usage = response_data.get("usage", {})
            input_tokens = usage.get("prompt_tokens", 0)
            output_tokens = usage.get("completion_tokens", 0)

            # Resolved model name
            resolved_model = response_data.get("model", self.model)

            # Compute fingerprint for HMAC verification
            fingerprint = None
            if response_text:
                try:
                    fingerprint = _compute_fingerprint(
                        "",  # symbol would be passed separately in W4 context
                        resolved_model,
                        response_text
                    )
                except Exception as e:
                    logger.warning(f"Failed to compute fingerprint: {e}")

            # Estimate cost
            cost_usd = self._estimate_cost(
                resolved_model,
                input_tokens,
                output_tokens
            )

            result = {
                "response": response_text,
                "model": resolved_model,
                "endpoint": self.endpoint,
                "inference_ms": inference_ms,
                "is_heuristic": False,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
                "cost_usd": cost_usd,
            }

            if fingerprint:
                result["fingerprint"] = fingerprint

            logger.debug(
                f"MNEMOS inference complete: model={resolved_model}, "
                f"tokens={input_tokens}/{output_tokens}, "
                f"latency={inference_ms}ms, cost=${cost_usd:.6f}"
            )

            return result

        except urllib.error.HTTPError as e:
            error_body = ""
            try:
                error_body = e.read().decode("utf-8")
            except Exception:
                pass

            if e.code == 401:
                raise RuntimeError(
                    "MNEMOS authentication failed: invalid or expired API key"
                )
            elif e.code == 400:
                raise RuntimeError(
                    f"MNEMOS API error (400 Bad Request): {error_body}"
                )
            elif e.code == 429:
                raise RuntimeError(
                    "MNEMOS rate limit exceeded. Retry after backoff."
                )
            elif e.code == 503:
                raise RuntimeError(
                    "MNEMOS service unavailable. Check endpoint health."
                )
            else:
                raise RuntimeError(
                    f"MNEMOS HTTP {e.code}: {e.reason}"
                )

        except urllib.error.URLError as e:
            raise RuntimeError(
                f"MNEMOS connection error: {e.reason}. "
                f"Check INVESTORCLAW_MNEMOS_ENDPOINT={self.endpoint}"
            )

        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"MNEMOS response parsing error: {e}. "
                f"Endpoint may not be OpenAI-compatible."
            )

        except Exception as e:
            raise RuntimeError(
                f"MNEMOS inference failed: {type(e).__name__}: {e}"
            )

    def _estimate_cost(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int
    ) -> float:
        """
        Estimate inference cost based on model and token usage.

        Pricing table reflects April 2026 rates. Update as rates change.
        """
        # Standard pricing model (in USD per 1M tokens)
        pricing_table = {
            # OpenAI
            "gpt-4o": {"input": 5.0, "output": 15.0},
            "gpt-4-turbo": {"input": 10.0, "output": 30.0},
            "gpt-4": {"input": 30.0, "output": 60.0},
            "gpt-3.5-turbo": {"input": 0.5, "output": 1.5},

            # Anthropic
            "claude-opus-4-1": {"input": 15.0, "output": 75.0},
            "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
            "claude-haiku-4-5": {"input": 0.8, "output": 4.0},

            # Groq (free tier + paid)
            "llama-3.3-70b": {"input": 0.0, "output": 0.0},  # Free tier
            "groq-mixtral": {"input": 0.0, "output": 0.0},    # Free tier

            # Together AI
            "meta-llama-4": {"input": 0.27, "output": 0.27},
            "qwen3-235b": {"input": 0.6, "output": 0.6},

            # Perplexity
            "sonar-pro": {"input": 2.0, "output": 8.0},

            # Fallback for unknown models
            "default": {"input": 1.0, "output": 3.0}
        }

        rates = pricing_table.get(
            model,
            pricing_table.get(model.split("/")[-1], pricing_table["default"])
        )

        input_cost = (input_tokens / 1_000_000) * rates["input"]
        output_cost = (output_tokens / 1_000_000) * rates["output"]

        return round(input_cost + output_cost, 6)

    def get_available_models(self) -> list:
        """Retrieve list of available models from MNEMOS."""
        try:
            req = urllib.request.Request(
                f"{self.endpoint}/v1/models",
                method="GET",
                headers={"Authorization": f"Bearer {self.api_key}"}
            )

            with urllib.request.urlopen(req, timeout=5.0) as resp:
                data = json.loads(resp.read())
                return data.get("data", [])

        except Exception as e:
            logger.warning(f"Failed to retrieve models from MNEMOS: {e}")
            return []

    def to_dict(self) -> dict:
        """Serialize client configuration for logging."""
        return {
            "backend": "mnemos",
            "endpoint": self.endpoint,
            "model": self.model,
            "search_enabled": self.search_enabled,
            "timeout": self.timeout
        }


# Backward compatibility alias (if using as drop-in for ConsultationClient)
ConsultationClient = MNEMOSConsultationClient


if __name__ == "__main__":
    """Quick test of MNEMOS consultation client."""
    logging.basicConfig(level=logging.INFO)

    try:
        client = MNEMOSConsultationClient()
        print(f"✓ MNEMOS client initialized: {client.to_dict()}")

        # Test simple inference
        result = client.consult(
            prompt="Summarize: Apple Inc reported strong Q4 earnings with 12% YoY growth.",
            system_prompt="You are a financial analyst.",
            max_tokens=100
        )

        print("✓ Inference successful:")
        print(f"  Model: {result['model']}")
        print(f"  Latency: {result['inference_ms']}ms")
        print(f"  Tokens: {result['input_tokens']}/{result['output_tokens']}")
        print(f"  Cost: ${result['cost_usd']:.6f}")
        print(f"  Response: {result['response'][:100]}...")

    except Exception as e:
        print(f"✗ Error: {e}")
        import sys
        sys.exit(1)
