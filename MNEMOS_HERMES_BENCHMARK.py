#!/usr/bin/env python3
"""
MNEMOS Hermes Agent Benchmark
Performance testing via hermes CLI to MNEMOS endpoint
"""

import subprocess
import json
import time
from statistics import median
from datetime import datetime
from pathlib import Path

ENDPOINT = "http://192.168.207.25:5002"
MODELS = [
    "mnemos-proteus/llama-3.3-70b-versatile",
    "mnemos-proteus/gpt-4o",
]

PROMPTS = [
    "What is 2+2?",
    "Explain quantum computing in 1 sentence.",
    "What is the capital of France?",
]

class HermesBenchmark:
    def __init__(self):
        self.results = {
            "latency": {},
            "throughput": {},
            "model_switching": [],
            "timestamp": datetime.now().isoformat(),
            "endpoint": ENDPOINT
        }

    def test_latency(self):
        """Test latency for each model via hermes CLI."""
        print("\n[1/3] LATENCY TESTS")
        print("-" * 70)

        for model in MODELS:
            latencies = []
            model_short = model.split('/')[-1]
            print(f"  Testing {model_short}...")

            for prompt in PROMPTS:
                try:
                    start = time.time()
                    # hermes chat -q "query" -Q (quiet mode, no banner)
                    result = subprocess.run(
                        ["hermes", "chat", "-q", prompt, "-Q"],
                        capture_output=True,
                        text=True,
                        timeout=120
                    )
                    latency_ms = (time.time() - start) * 1000
                    latencies.append(latency_ms)

                    if result.returncode == 0:
                        print(f"    ✓ {prompt[:35]:35s} → {latency_ms:7.1f}ms")
                    else:
                        print(f"    ✗ {prompt[:35]:35s} → Exit code {result.returncode}")
                except Exception as e:
                    print(f"    ✗ {prompt[:35]:35s} → Error: {str(e)[:40]}")

            if latencies:
                self.results["latency"][model_short] = {
                    "avg_ms": sum(latencies) / len(latencies),
                    "min_ms": min(latencies),
                    "max_ms": max(latencies),
                    "median_ms": median(latencies),
                    "samples": len(latencies)
                }

    def test_throughput(self):
        """Test concurrent requests via hermes."""
        print("\n[2/3] THROUGHPUT TEST")
        print("-" * 70)

        model = MODELS[0]
        model_short = model.split('/')[-1]
        duration = 10

        print(f"  Testing sequential requests for {duration}s with {model_short}...")

        start_time = time.time()
        request_times = []
        success_count = 0

        while time.time() - start_time < duration:
            try:
                req_start = time.time()
                result = subprocess.run(
                    ["hermes", "chat", "-q", "ping", "-Q"],
                    capture_output=True,
                    text=True,
                    timeout=30
                )
                req_time = (time.time() - req_start) * 1000
                request_times.append(req_time)

                if result.returncode == 0:
                    success_count += 1
            except Exception as e:
                pass

        elapsed = time.time() - start_time

        if request_times:
            request_times.sort()
            throughput = success_count / elapsed
            print(f"    Completed: {success_count} successful requests in {elapsed:.1f}s")
            print(f"    Throughput: {throughput:.2f} req/sec")
            print(f"    Latency: avg={sum(request_times)/len(request_times):.1f}ms, p95={request_times[int(len(request_times)*0.95)]:.1f}ms")

            self.results["throughput"] = {
                "successful_requests": success_count,
                "total_duration_sec": elapsed,
                "throughput_req_per_sec": throughput,
                "avg_latency_ms": sum(request_times) / len(request_times),
                "p95_latency_ms": request_times[int(len(request_times)*0.95)]
            }

    def run(self):
        """Run all tests."""
        print(f"\n{'='*70}")
        print(f"MNEMOS Hermes CLI Benchmark")
        print(f"{'='*70}")
        print(f"Endpoint: {ENDPOINT}")

        try:
            self.test_latency()
            self.test_throughput()
        finally:
            pass

        self.print_summary()
        self.save_results()

    def print_summary(self):
        """Print results summary."""
        print(f"\n{'='*70}")
        print("BENCHMARK RESULTS")
        print(f"{'='*70}")

        print("\nLatency Summary:")
        for model, metrics in self.results["latency"].items():
            print(f"  {model}:")
            print(f"    Avg: {metrics['avg_ms']:.1f}ms | Min: {metrics['min_ms']:.1f}ms | Max: {metrics['max_ms']:.1f}ms | Median: {metrics['median_ms']:.1f}ms")

        if "throughput" in self.results:
            t = self.results["throughput"]
            print(f"\nThroughput Summary:")
            print(f"  {t['throughput_req_per_sec']:.2f} req/sec | Avg latency: {t['avg_latency_ms']:.1f}ms | P95: {t['p95_latency_ms']:.1f}ms")

    def save_results(self):
        """Save results to JSON."""
        output_dir = Path("/Users/jasonperlow/Projects/mnemos-prod-working/benchmark_results")
        output_dir.mkdir(parents=True, exist_ok=True)

        output_file = output_dir / f"benchmark_hermes_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"

        with open(output_file, 'w') as f:
            json.dump(self.results, f, indent=2)

        print(f"\n✓ Results saved: {output_file}")


if __name__ == "__main__":
    benchmark = HermesBenchmark()
    benchmark.run()
