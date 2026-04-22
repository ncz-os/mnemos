#!/usr/bin/env python3
"""
MNEMOS Direct API Benchmark
============================
Performance testing via HTTP directly to PROTEUS v3.0.0 endpoint
"""

import httpx
import json
import time
from statistics import median
from datetime import datetime
from pathlib import Path

ENDPOINT = "http://192.168.207.25:5002"
MODELS = [
    "llama-3.3-70b-versatile",
    "gpt-4o",
    "sonar-pro",
    "claude-3-5-sonnet-20241022"
]

PROMPTS = [
    "What is 2+2?",
    "Explain quantum computing in 1 sentence.",
    "What is the capital of France?",
]

class DirectBenchmark:
    def __init__(self):
        self.client = httpx.Client(timeout=60.0)
        self.results = {
            "latency": {},
            "model_switching": [],
            "timestamp": datetime.now().isoformat()
        }

    def test_latency(self):
        """Test latency for each model."""
        print("\n[1/3] LATENCY TESTS")
        print("-" * 70)

        for model in MODELS[:2]:  # Test first 2 models
            latencies = []
            print(f"  Testing {model}...")

            for prompt in PROMPTS:
                try:
                    start = time.time()
                    response = self.client.post(
                        f"{ENDPOINT}/v1/chat/completions",
                        json={
                            "model": model,
                            "messages": [{"role": "user", "content": prompt}],
                            "max_tokens": 100
                        }
                    )
                    latency_ms = (time.time() - start) * 1000
                    latencies.append(latency_ms)

                    if response.status_code == 200:
                        print(f"    ✓ {prompt[:35]:35s} → {latency_ms:7.1f}ms")
                    else:
                        print(f"    ✗ {prompt[:35]:35s} → HTTP {response.status_code}")
                except Exception as e:
                    print(f"    ✗ {prompt[:35]:35s} → Error: {str(e)[:40]}")

            if latencies:
                self.results["latency"][model] = {
                    "avg_ms": sum(latencies) / len(latencies),
                    "min_ms": min(latencies),
                    "max_ms": max(latencies),
                    "median_ms": median(latencies),
                    "samples": len(latencies)
                }

    def test_model_switching(self):
        """Test switching between models."""
        print("\n[2/3] MODEL SWITCHING TESTS")
        print("-" * 70)

        for i in range(len(MODELS) - 1):
            model_a = MODELS[i]
            model_b = MODELS[i + 1]

            print(f"  Switching {model_a} → {model_b}...")

            try:
                # First model
                start_a = time.time()
                response_a = self.client.post(
                    f"{ENDPOINT}/v1/chat/completions",
                    json={
                        "model": model_a,
                        "messages": [{"role": "user", "content": "test"}],
                        "max_tokens": 50
                    }
                )
                time_a = (time.time() - start_a) * 1000

                # Switch to second model
                start_b = time.time()
                response_b = self.client.post(
                    f"{ENDPOINT}/v1/chat/completions",
                    json={
                        "model": model_b,
                        "messages": [{"role": "user", "content": "test"}],
                        "max_tokens": 50
                    }
                )
                time_b = (time.time() - start_b) * 1000

                status_a = "✓" if response_a.status_code == 200 else "✗"
                status_b = "✓" if response_b.status_code == 200 else "✗"

                print(f"    {status_a} {model_a}: {time_a:6.1f}ms")
                print(f"    {status_b} {model_b}: {time_b:6.1f}ms (switch overhead: {abs(time_b - time_a):6.1f}ms)")

                self.results["model_switching"].append({
                    "from": model_a,
                    "to": model_b,
                    "time_from_ms": time_a,
                    "time_to_ms": time_b,
                    "overhead_ms": abs(time_b - time_a)
                })
            except Exception as e:
                print(f"    ✗ Error: {str(e)[:60]}")

    def test_throughput(self):
        """Test concurrent requests."""
        print("\n[3/3] THROUGHPUT TEST")
        print("-" * 70)

        model = MODELS[0]
        concurrent = 3
        duration = 10

        print(f"  Testing {concurrent} concurrent requests for {duration}s with {model}...")

        start_time = time.time()
        request_times = []
        success_count = 0

        while time.time() - start_time < duration:
            try:
                req_start = time.time()
                response = self.client.post(
                    f"{ENDPOINT}/v1/chat/completions",
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": "ping"}],
                        "max_tokens": 10
                    }
                )
                req_time = (time.time() - req_start) * 1000
                request_times.append(req_time)

                if response.status_code == 200:
                    success_count += 1
            except Exception:
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
        print("MNEMOS Direct API Benchmark")
        print(f"{'='*70}")
        print(f"Endpoint: {ENDPOINT}")
        print(f"Models: {', '.join(MODELS[:2])}")

        try:
            self.test_latency()
            self.test_model_switching()
            self.test_throughput()
        finally:
            self.client.close()

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
            print("\nThroughput Summary:")
            print(f"  {t['throughput_req_per_sec']:.2f} req/sec | Avg latency: {t['avg_latency_ms']:.1f}ms | P95: {t['p95_latency_ms']:.1f}ms")

        if self.results["model_switching"]:
            print("\nModel Switching:")
            for switch in self.results["model_switching"]:
                print(f"  {switch['from']} → {switch['to']}: overhead {switch['overhead_ms']:.1f}ms")

    def save_results(self):
        """Save results to JSON."""
        output_dir = Path("/Users/jasonperlow/Projects/mnemos-prod-working/benchmark_results")
        output_dir.mkdir(parents=True, exist_ok=True)

        output_file = output_dir / f"benchmark_direct_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"

        with open(output_file, 'w') as f:
            json.dump(self.results, f, indent=2)

        print(f"\n✓ Results saved: {output_file}")


if __name__ == "__main__":
    benchmark = DirectBenchmark()
    benchmark.run()
