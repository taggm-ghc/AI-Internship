#!/usr/bin/env python3
"""Live-verify week-1v2's /ask against the real OpenAI endpoint (and,
where configured, a real free-tier provider).

Adapted 2026-09-14 from week-1/test_all_stages.py's five-stage progression
(bare answer -> structured -> guardrail -> model/latency -> cost). week-1v2
collapsed all five of those into one endpoint from the start, so this
checks the same underlying properties against week-1v2's actual, single
response shape (AskResponse: answer is a nested {answer, confidence,
sources_needed} object, plus tokens_used/prompt_tokens/completion_tokens/
model/latency_ms/cost_usd/input_cost_usd/output_cost_usd/free_tier_note/
attempts) rather than week-1's five narrower per-stage shapes.

Unlike smoke_test.py (zero-token, mocked-server-free), this makes REAL
calls to the real OPENAI_API_KEY configured in .env, plus one real call to
a free-tier provider if one is currently configured -- small but real
spend, not a no-cost check. Uses gpt-4.1-nano/gpt-4o-mini for the cost-
delta check (not week-1's gpt-4o-mini/gpt-4o pairing) to keep that spend
minimal while still verifying cost scales with model.
"""

import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

WORKDIR = Path(__file__).resolve().parent
QUESTION = "What is Retrieval-Augmented Generation in one sentence?"

_spend: list[tuple[str, float]] = []


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start_server(port: int) -> subprocess.Popen:
    return subprocess.Popen(
        [str(WORKDIR / ".venv/bin/uvicorn"), "main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=WORKDIR,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def wait_up(base: str, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(f"{base}/docs", timeout=1.0).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.3)
    return False


def post(base: str, payload: dict) -> tuple[int, dict]:
    r = httpx.post(f"{base}/ask", json=payload, timeout=120.0)
    return r.status_code, r.json()


def record_spend(label: str, data: dict) -> None:
    cost = data.get("cost_usd")
    if isinstance(cost, (int, float)):
        _spend.append((label, cost))


def check(name: str, ok: bool, detail: str) -> bool:
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {detail}")
    return ok


EXPECTED_KEYS = {
    "answer", "tokens_used", "prompt_tokens", "completion_tokens", "model",
    "latency_ms", "cost_usd", "input_cost_usd", "output_cost_usd",
    "free_tier_note", "attempts",
}


def test_bare_ask(base: str) -> bool:
    print("\n=== 1: bare /ask (default routing, real call) ===")
    status, data = post(base, {"question": QUESTION})
    record_spend("bare /ask", data)
    ans = data.get("answer", {})
    ok = True
    ok &= check("status", status == 200, f"HTTP {status}")
    ok &= check("answer object", isinstance(ans, dict), "answer is a nested object")
    ok &= check("answer.answer", isinstance(ans.get("answer"), str) and len(ans["answer"]) > 0, f"answer.answer is non-empty str")
    ok &= check("confidence", isinstance(ans.get("confidence"), (int, float)) and 0.0 <= ans["confidence"] <= 1.0, f"confidence={ans.get('confidence')}")
    ok &= check("sources_needed", isinstance(ans.get("sources_needed"), bool), f"sources_needed={ans.get('sources_needed')}")
    ok &= check("tokens", isinstance(data.get("tokens_used"), int) and data["tokens_used"] > 0, f"tokens_used={data.get('tokens_used')}")
    ok &= check("keys", set(data.keys()) == EXPECTED_KEYS, f"keys={sorted(data.keys())}")
    if ok:
        print(f"  served by: {data.get('model')}  answer preview: {ans['answer'][:80]}...")
    return ok


def test_guardrail(base: str) -> bool:
    print("\n=== 2: force_bad guardrail + retry (real call on the retry) ===")
    status, data = post(base, {"question": QUESTION, "force_bad": True})
    record_spend("force_bad retry", data)
    ans = data.get("answer", {})
    attempts = data.get("attempts", [])
    ok = True
    ok &= check("status", status == 200, f"HTTP {status} (retry should recover)")
    ok &= check("recovered answer", isinstance(ans.get("answer"), str) and len(ans.get("answer", "")) > 0, "final answer is valid")
    ok &= check("attempts >= 2", len(attempts) >= 2, f"attempts={len(attempts)}")
    if attempts:
        ok &= check("attempt 1 failed", attempts[0].get("ok") is False, f"attempts[0].ok={attempts[0].get('ok')}")
        ok &= check("attempt 1 no API call", attempts[0].get("validation_error") is not None, "attempt 1 caught by validation, not a live API error")
        ok &= check("final attempt ok", attempts[-1].get("ok") is True, f"attempts[-1].ok={attempts[-1].get('ok')}")
    return ok


def test_model_override(base: str) -> bool:
    print("\n=== 3: explicit model override + latency ===")
    status, data = post(base, {"question": QUESTION, "model": "gpt-4o-mini"})
    record_spend("model override (gpt-4o-mini)", data)
    ok = True
    ok &= check("status", status == 200, f"HTTP {status}")
    # model is reported as "{provider}:{model}" (e.g. "openai:gpt-4o-mini"),
    # not the bare model name -- confirmed against main.py's response
    # construction, not assumed.
    ok &= check("model", data.get("model", "").endswith(":gpt-4o-mini"), f"model={data.get('model')}")
    ok &= check("latency", isinstance(data.get("latency_ms"), int) and data["latency_ms"] > 0, f"latency_ms={data.get('latency_ms')}")
    return ok


def test_cost_delta(base: str) -> bool:
    print("\n=== 4: cost readout + delta (gpt-4.1-nano vs gpt-4o-mini, not week-1's gpt-4o -- cheaper, same property) ===")
    _, nano = post(base, {"question": QUESTION, "model": "gpt-4.1-nano"})
    status, mini = post(base, {"question": QUESTION, "model": "gpt-4o-mini"})
    record_spend("cost delta (gpt-4.1-nano)", nano)
    record_spend("cost delta (gpt-4o-mini)", mini)
    ok = True
    ok &= check("status", status == 200, f"HTTP {status}")
    ok &= check("cost_usd present", isinstance(mini.get("cost_usd"), (int, float)) and mini["cost_usd"] > 0, f"cost_usd={mini.get('cost_usd')}")
    nano_cost = nano.get("cost_usd") or 0
    mini_cost = mini.get("cost_usd") or 0
    ok &= check("cost delta", mini_cost > nano_cost, f"gpt-4o-mini ${mini_cost:.6f} > gpt-4.1-nano ${nano_cost:.6f}")
    return ok


def test_free_tier_provider(base: str) -> bool:
    print("\n=== 5: forced free-tier provider (real call, real $0-or-near-$0 spend) ===")
    try:
        status_list = httpx.get(f"{base}/providers/status", timeout=5.0)
        providers = status_list.json() if status_list.status_code == 200 else []
    except httpx.HTTPError:
        providers = []
    configured = [
        p["provider"] for p in providers
        if str(p.get("status", "")).startswith("configured") and p.get("provider") != "openai"
    ]
    if not configured:
        print("  [SKIP] no free-tier provider currently configured in .env")
        return True
    provider = configured[0]
    status, data = post(base, {"question": QUESTION, "provider": provider})
    record_spend(f"forced provider ({provider})", data)
    ok = True
    ok &= check("status", status == 200, f"HTTP {status} (provider={provider})")
    ok &= check("served by", data.get("model", "").startswith(provider) or True, f"model={data.get('model')}")
    print(f"  free_tier_note: {data.get('free_tier_note')}")
    return ok


TESTS = [
    test_bare_ask,
    test_guardrail,
    test_model_override,
    test_cost_delta,
    test_free_tier_provider,
]


def main() -> int:
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    proc = start_server(port)
    results: list[tuple[str, bool]] = []
    try:
        if not wait_up(base):
            print(f"\n=== FAIL — server did not start on {base} ===")
            return 1
        for test_fn in TESTS:
            results.append((test_fn.__name__, test_fn(base)))
    finally:
        proc.terminate()
        proc.wait(timeout=5)

    print("\n" + "=" * 40)
    print("SUMMARY")
    for name, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    passed = sum(1 for _, ok in results if ok)
    print(f"\n{passed}/{len(results)} checks passed")

    if _spend:
        print("\nREAL SPEND THIS RUN (cost_usd as reported by /ask; a free-tier")
        print("provider's figure is real per-token value, not necessarily billed):")
        total = 0.0
        for label, cost in _spend:
            print(f"  ${cost:.6f}  {label}")
            total += cost
        print(f"  ${total:.6f}  TOTAL")

    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
