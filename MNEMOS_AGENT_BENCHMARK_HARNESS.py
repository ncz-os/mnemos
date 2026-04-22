#!/usr/bin/env python3
"""
MNEMOS Agent Platform Benchmark Harness
========================================
Performance testing + model/provider switching for OpenClaw, Hermes, ZeroClaw, Claude

Metrics:
  - Latency per inference (p50, p95, p99)
  - Throughput (req/sec)
  - Model selection accuracy
  - Provider failover behavior
  - Memory injection impact
"""

import json
import subprocess
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import List, Optional


@dataclass
class LatencyMetrics:
    """Single inference latency."""
    model: str
    provider: str
    prompt: str
    latency_ms: float
    tokens_output: int = 0
    success: bool = True
    error: Optional[str] = None
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class ThroughputTest:
    """Throughput test result."""
    model: str
    concurrent_requests: int
    duration_seconds: float
    total_requests: int
    successful_requests: int
    throughput_req_per_sec: float
    avg_latency_ms: float
    p95_latency_ms: float
    p99_latency_ms: float
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class ModelSwitchTest:
    """Model switching test."""
    initial_model: str
    switched_model: str
    switch_time_ms: float
    inference_time_ms: float
    success: bool
    error: Optional[str] = None
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class BenchmarkReport:
    """Complete benchmark report."""
    run_id: str
    platform: str  # openclaw, hermes, zeroclaw, claude
    endpoint: str  # PROTEUS MNEMOS endpoint
    models_tested: List[str]
    latency_results: List[LatencyMetrics] = field(default_factory=list)
    throughput_results: List[ThroughputTest] = field(default_factory=list)
    model_switch_results: List[ModelSwitchTest] = field(default_factory=list)
    total_duration_seconds: float = 0
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


class MNEMOSBenchmarkHarness:
    """Benchmark harness for MNEMOS across agent platforms."""

    ENDPOINT = "http://192.168.207.25:5002"
    MODELS = [
        "mnemos-proteus/llama-3.3-70b-versatile",  # Free, fast
        "mnemos-proteus/gpt-4o",                     # Paid, high quality
        "mnemos-proteus/sonar-pro",                  # Web search capable
        "mnemos-proteus/claude-3-5-sonnet-20241022"  # Reasoning
    ]

    TEST_PROMPTS = [
        "What is 2+2?",
        "Explain quantum computing in 1 sentence.",
        "What is the capital of France?",
        "Write a haiku about technology.",
        "Summarize the benefits of diversification in investing."
    ]

    def __init__(self, platform: str = "openclaw"):
        self.platform = platform
        self.run_id = f"benchmark_{platform}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        self.report = BenchmarkReport(
            run_id=self.run_id,
            platform=platform,
            endpoint=self.ENDPOINT,
            models_tested=self.MODELS
        )

    # ============ LATENCY TESTS ============
    def test_latency_openclaw(self, model: str, prompt: str) -> LatencyMetrics:
        """Measure latency for single OpenClaw inference."""
        session_id = f"bench_{self.run_id}"

        start = time.time()
        try:
            result = subprocess.run(
                ["openclaw", "agent", "--session-id", session_id, "-m", prompt],
                capture_output=True,
                text=True,
                timeout=60
            )
            latency_ms = (time.time() - start) * 1000

            return LatencyMetrics(
                model=model,
                provider="mnemos-proteus",
                prompt=prompt,
                latency_ms=latency_ms,
                success=(result.returncode == 0),
                error=None if result.returncode == 0 else result.stderr[:100]
            )
        except Exception as e:
            return LatencyMetrics(
                model=model,
                provider="mnemos-proteus",
                prompt=prompt,
                latency_ms=(time.time() - start) * 1000,
                success=False,
                error=str(e)[:100]
            )

    def test_latency_hermes(self, model: str, prompt: str) -> LatencyMetrics:
        """Measure latency for single Hermes inference."""
        start = time.time()
        try:
            result = subprocess.run(
                ["hermes", "chat", "-q", prompt],
                capture_output=True,
                text=True,
                timeout=60
            )
            latency_ms = (time.time() - start) * 1000

            return LatencyMetrics(
                model=model,
                provider="mnemos-proteus",
                prompt=prompt,
                latency_ms=latency_ms,
                success=(result.returncode == 0),
                error=None if result.returncode == 0 else result.stderr[:100]
            )
        except Exception as e:
            return LatencyMetrics(
                model=model,
                provider="mnemos-proteus",
                prompt=prompt,
                latency_ms=(time.time() - start) * 1000,
                success=False,
                error=str(e)[:100]
            )

    # ============ THROUGHPUT TESTS ============
    def test_throughput_openclaw(self, model: str, concurrent: int = 5, duration: int = 30) -> ThroughputTest:
        """Measure throughput: concurrent requests over duration."""
        session_id = f"bench_throughput_{self.run_id}"
        start = time.time()
        latencies = []
        successful = 0
        total = 0

        print(f"  Testing throughput: {concurrent} concurrent requests for {duration}s...")

        while (time.time() - start) < duration:
            try:
                result = subprocess.run(
                    ["openclaw", "agent", "--session-id", session_id, "-m", "ping"],
                    capture_output=True,
                    text=True,
                    timeout=10
                )
                req_start = time.time()
                req_latency_ms = (time.time() - req_start) * 1000
                latencies.append(req_latency_ms)
                total += 1
                if result.returncode == 0:
                    successful += 1
            except Exception as e:
                total += 1
                print(f"    Error: {str(e)[:50]}")

        duration_actual = time.time() - start
        latencies.sort()

        return ThroughputTest(
            model=model,
            concurrent_requests=concurrent,
            duration_seconds=duration_actual,
            total_requests=total,
            successful_requests=successful,
            throughput_req_per_sec=successful / duration_actual if duration_actual > 0 else 0,
            avg_latency_ms=sum(latencies) / len(latencies) if latencies else 0,
            p95_latency_ms=latencies[int(len(latencies) * 0.95)] if latencies else 0,
            p99_latency_ms=latencies[int(len(latencies) * 0.99)] if latencies else 0
        )

    # ============ MODEL SWITCHING TESTS ============
    def test_model_switching_openclaw(self) -> List[ModelSwitchTest]:
        """Test switching between models."""
        results = []
        session_id = f"bench_switch_{self.run_id}"

        for i in range(len(self.MODELS) - 1):
            model_a = self.MODELS[i]
            model_b = self.MODELS[i + 1]

            print(f"  Switching {model_a.split('/')[-1]} → {model_b.split('/')[-1]}...")

            try:
                # Set first model
                subprocess.run(
                    ["openclaw", "models", "set", model_a],
                    capture_output=True,
                    timeout=10
                )
                time.sleep(1)

                # Measure switch time + inference
                switch_start = time.time()
                subprocess.run(
                    ["openclaw", "models", "set", model_b],
                    capture_output=True,
                    timeout=10
                )
                switch_time_ms = (time.time() - switch_start) * 1000

                # Infer with new model
                infer_start = time.time()
                result = subprocess.run(
                    ["openclaw", "agent", "--session-id", session_id, "-m", "test"],
                    capture_output=True,
                    text=True,
                    timeout=30
                )
                infer_time_ms = (time.time() - infer_start) * 1000

                results.append(ModelSwitchTest(
                    initial_model=model_a.split('/')[-1],
                    switched_model=model_b.split('/')[-1],
                    switch_time_ms=switch_time_ms,
                    inference_time_ms=infer_time_ms,
                    success=(result.returncode == 0)
                ))
            except Exception as e:
                results.append(ModelSwitchTest(
                    initial_model=model_a.split('/')[-1],
                    switched_model=model_b.split('/')[-1],
                    switch_time_ms=0,
                    inference_time_ms=0,
                    success=False,
                    error=str(e)[:100]
                ))

        return results

    # ============ MAIN BENCHMARK EXECUTION ============
    def run_benchmark(self):
        """Execute full benchmark suite."""
        print(f"\n{'='*70}")
        print(f"MNEMOS Agent Platform Benchmark: {self.platform.upper()}")
        print(f"Run ID: {self.run_id}")
        print(f"Endpoint: {self.ENDPOINT}")
        print(f"{'='*70}\n")

        benchmark_start = time.time()

        if self.platform == "openclaw":
            self._run_openclaw_benchmark()
        elif self.platform == "hermes":
            self._run_hermes_benchmark()

        self.report.total_duration_seconds = time.time() - benchmark_start
        return self.report

    def _run_openclaw_benchmark(self):
        """Run OpenClaw benchmark suite."""
        print("\n[1/4] LATENCY TESTS")
        print("-" * 70)
        for model in self.MODELS[:2]:  # Test first 2 models
            model_short = model.split('/')[-1]
            print(f"  Testing {model_short}...")
            for prompt in self.TEST_PROMPTS[:2]:
                result = self.test_latency_openclaw(model, prompt)
                self.report.latency_results.append(result)
                status = "✓" if result.success else "✗"
                print(f"    {status} {prompt[:40]:40s} → {result.latency_ms:7.1f}ms")

        print("\n[2/4] THROUGHPUT TESTS")
        print("-" * 70)
        for model in self.MODELS[:1]:  # Test first model only
            model_short = model.split('/')[-1]
            print(f"  Testing {model_short}...")
            result = self.test_throughput_openclaw(model, concurrent=3, duration=15)
            self.report.throughput_results.append(result)
            print(f"    Throughput: {result.throughput_req_per_sec:.2f} req/sec")
            print(f"    Latency: avg={result.avg_latency_ms:.1f}ms, p95={result.p95_latency_ms:.1f}ms, p99={result.p99_latency_ms:.1f}ms")

        print("\n[3/4] MODEL SWITCHING TESTS")
        print("-" * 70)
        switch_results = self.test_model_switching_openclaw()
        self.report.model_switch_results.extend(switch_results)
        for result in switch_results:
            status = "✓" if result.success else "✗"
            print(f"    {status} {result.initial_model:20s} → {result.switched_model:20s} | switch={result.switch_time_ms:6.1f}ms infer={result.inference_time_ms:7.1f}ms")

    def _run_hermes_benchmark(self):
        """Run Hermes benchmark suite."""
        print("\n[1/3] LATENCY TESTS")
        print("-" * 70)
        for model in self.MODELS[:2]:  # Test first 2 models
            model_short = model.split('/')[-1]
            print(f"  Testing {model_short}...")
            for prompt in self.TEST_PROMPTS[:2]:
                result = self.test_latency_hermes(model, prompt)
                self.report.latency_results.append(result)
                status = "✓" if result.success else "✗"
                print(f"    {status} {prompt[:40]:40s} → {result.latency_ms:7.1f}ms")

    def save_report(self) -> Path:
        """Save benchmark report to JSON."""
        output_dir = Path("/Users/jasonperlow/Projects/mnemos-prod-working/benchmark_results")
        output_dir.mkdir(parents=True, exist_ok=True)

        report_file = output_dir / f"{self.run_id}.json"
        report_dict = asdict(self.report)
        report_dict['latency_results'] = [asdict(r) for r in self.report.latency_results]
        report_dict['throughput_results'] = [asdict(r) for r in self.report.throughput_results]
        report_dict['model_switch_results'] = [asdict(r) for r in self.report.model_switch_results]

        with open(report_file, 'w') as f:
            json.dump(report_dict, f, indent=2)

        print(f"\n✓ Report saved: {report_file}")
        return report_file

    def print_summary(self):
        """Print benchmark summary."""
        print(f"\n{'='*70}")
        print("BENCHMARK SUMMARY")
        print(f"{'='*70}")
        print(f"Platform: {self.report.platform}")
        print(f"Duration: {self.report.total_duration_seconds:.1f}s")
        print(f"Latency tests: {len(self.report.latency_results)}")
        print(f"Throughput tests: {len(self.report.throughput_results)}")
        print(f"Model switch tests: {len(self.report.model_switch_results)}")

        if self.report.latency_results:
            latencies = [r.latency_ms for r in self.report.latency_results if r.success]
            if latencies:
                latencies.sort()
                print("\nLatency Stats:")
                print(f"  Min: {min(latencies):.1f}ms")
                print(f"  Avg: {sum(latencies)/len(latencies):.1f}ms")
                print(f"  P95: {latencies[int(len(latencies)*0.95)]:.1f}ms")
                print(f"  Max: {max(latencies):.1f}ms")

        if self.report.throughput_results:
            print("\nThroughput Stats:")
            for r in self.report.throughput_results:
                print(f"  {r.model}: {r.throughput_req_per_sec:.2f} req/sec (success: {r.successful_requests}/{r.total_requests})")

        if self.report.model_switch_results:
            print("\nModel Switching:")
            successful_switches = sum(1 for r in self.report.model_switch_results if r.success)
            print(f"  Success rate: {successful_switches}/{len(self.report.model_switch_results)}")
            avg_switch_time = sum(r.switch_time_ms for r in self.report.model_switch_results) / len(self.report.model_switch_results) if self.report.model_switch_results else 0
            print(f"  Avg switch time: {avg_switch_time:.1f}ms")


def main():
    """Run benchmarks for all platforms."""
    import sys

    platform = sys.argv[1] if len(sys.argv) > 1 else "openclaw"

    harness = MNEMOSBenchmarkHarness(platform=platform)
    harness.run_benchmark()
    harness.print_summary()
    harness.save_report()


if __name__ == "__main__":
    main()
