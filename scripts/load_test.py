import argparse
import json
import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def request_json(url: str, payload: dict | None = None) -> tuple[int, dict]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="GET" if body is None else "POST",
    )
    with urlopen(request, timeout=15) as response:
        response_body = response.read()
        result = json.loads(response_body) if response_body else {}
        return response.status, result


def provision_dial_workload(
    base_url: str,
    agent_count: int,
    borrower_count: int,
    mode: str,
) -> int:
    for index in range(agent_count):
        request_json(f"{base_url}/agents", {"name": f"load-agent-{index}"})
    for index in range(borrower_count):
        request_json(
            f"{base_url}/borrowers",
            {"name": f"load-borrower-{index}", "phone": f"+1555{index:010d}"},
        )
    _, campaign = request_json(
        f"{base_url}/campaigns",
        {"name": "Load-test campaign", "mode": mode, "answer_rate": 0.5},
    )
    return campaign["id"]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Reproducible HTTP load test for health and dial reservation endpoints."
    )
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--requests", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--workload", choices=("health", "dial", "mixed"), default="mixed")
    parser.add_argument("--agents", type=int)
    parser.add_argument("--borrowers", type=int)
    parser.add_argument("--mode", choices=("PROGRESSIVE", "PREDICTIVE"), default="PROGRESSIVE")
    args = parser.parse_args()
    if args.requests < 1 or args.concurrency < 1:
        parser.error("--requests and --concurrency must both be positive")

    base_url = args.url.rstrip("/")
    campaign_id = None
    if args.workload in {"dial", "mixed"}:
        dial_requests = args.requests if args.workload == "dial" else (args.requests + 1) // 2
        agents = args.agents if args.agents is not None else dial_requests
        borrowers = args.borrowers if args.borrowers is not None else dial_requests
        if agents < 1 or borrowers < 1:
            parser.error("--agents and --borrowers must be positive")
        campaign_id = provision_dial_workload(
            base_url, agents, borrowers, args.mode
        )

    def execute(index: int) -> tuple[bool, float, str | None]:
        started = time.perf_counter()
        try:
            is_health = args.workload == "health" or (
                args.workload == "mixed" and index % 2 == 1
            )
            if is_health:
                status, _ = request_json(f"{base_url}/health")
                return 200 <= status < 400, time.perf_counter() - started, None
            assert campaign_id is not None
            status, result = request_json(
                f"{base_url}/campaigns/{campaign_id}/dial",
                {"campaign_id": campaign_id, "max_calls": 1},
            )
            if status < 200 or status >= 400 or result.get("safety_approved_calls") != 1:
                return False, time.perf_counter() - started, (
                    f"Dial request did not reserve one call: {result}"
                )
            return True, time.perf_counter() - started, None
        except (HTTPError, URLError, TimeoutError, ValueError, KeyError) as exc:
            return False, time.perf_counter() - started, f"{type(exc).__name__}: {exc}"

    start = time.perf_counter()
    latencies = []
    errors = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        futures = [executor.submit(execute, index) for index in range(args.requests)]
        for future in as_completed(futures):
            succeeded, elapsed, error = future.result()
            latencies.append(elapsed)
            if not succeeded:
                errors.append(error or "HTTP request failed")
    elapsed = time.perf_counter() - start
    latencies.sort()
    p95_index = max(0, math.ceil(0.95 * len(latencies)) - 1)
    print(json.dumps({
        "workload": args.workload,
        "requests": args.requests,
        "concurrency": args.concurrency,
        "duration_seconds": round(elapsed, 3),
        "throughput_requests_per_second": round(args.requests / elapsed, 2),
        "failures": len(errors),
        "errors": len(errors),
        "p95_latency_ms": round(latencies[p95_index] * 1000, 2),
        "error_samples": errors[:5],
    }, indent=2))


if __name__ == "__main__":
    main()
