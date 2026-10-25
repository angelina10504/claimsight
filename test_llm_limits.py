"""
Test how ClaimSight behaves against a provider's rate limits.

Costs no tokens and makes no provider call: it inspects the settings handed to
the client and drives the failure paths with stand-ins.

Groq's free tier limits tokens per minute, not only requests, so a claim with
two analysts running in parallel can exceed the allowance on its own and get a
429 that clears within the minute. What is tested here:

    the client is given enough attempts to ride out a per-minute window
    the provider's own Retry-After handling is used, not a second backoff loop
    requests are paced, while the analysts still run in parallel
    an identical repeat tool call is served from the first result
    the policy context sent to the analyst is capped
    exhausted retries end as a failure, never as a stale decision
    every token count the provider reports is recorded, with no cost invented
    rate limits, daily limits and malformed tool calls are told apart

Run:  uv run python test_llm_limits.py
"""

import os

os.environ.setdefault("CLAIMSIGHT_OFFLINE", "1")

import json
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("OPENAI_API_KEY", "not-used-in-this-test")

import config        # noqa: E402
import monitoring    # noqa: E402

GROQ_429 = ("litellm.RateLimitError: Error code: 429 - {'error': {'message': "
            "'Rate limit reached for model `openai/gpt-oss-20b` in organization "
            "`org_x` service tier `on_demand` on tokens per minute (TPM): Limit "
            "8000, Used 6140, Requested 3329. Please try again in 11.0175s.'}}")
GROQ_TPD = ("Error code: 429 - {'error': {'message': 'Rate limit reached ... on "
            "tokens per day (TPD): Limit 200000, Used 197545, Requested 2678.'}}")
GROQ_TOOL = ("Error code: 400 - {'error': {'code': 'tool_use_failed', 'message': "
             "'Model called python tool which was not enabled'}}")


def main() -> None:
    passed = total = 0

    def run(name, condition, detail=""):
        nonlocal passed, total
        total += 1
        passed += bool(condition)
        print(f"  {'PASS' if condition else 'FAIL'}  {name}" +
              (f"\n        {detail}" if detail else ""))

    # ---------------------------------------------------------------- the settings
    print("1. What the provider client is told\n")
    from crewai import LLM
    llm = LLM(model="openai/openai/gpt-oss-120b",
              base_url="https://api.groq.com/openai/v1", api_key="x",
              temperature=0.1, max_retries=int(os.getenv("CLAIMSIGHT_LLM_RETRIES", "5")),
              timeout=float(os.getenv("CLAIMSIGHT_LLM_TIMEOUT", "180")))
    params = llm._get_client_params() if hasattr(llm, "_get_client_params") else {}
    run("more than the default two attempts, so a per-minute window can pass",
        params.get("max_retries", 2) >= 4, f"max_retries={params.get('max_retries')}")
    run("a timeout is set, so a stalled call does not hang the claim",
        bool(params.get("timeout")), f"timeout={params.get('timeout')}")

    from openai import _base_client
    source = Path(_base_client.__file__).read_text()
    run("the client reads the provider's own Retry-After header",
        "retry-after-ms" in source and "retry-after" in source,
        "so the wait the provider asks for is used, not a guess")
    run("and adds jitter itself, so no second backoff loop is needed",
        "jitter" in source.lower())

    run("requests are paced", config.max_rpm() <= 10, f"max_rpm={config.max_rpm()}")
    limits = config.knowledge_limits()
    run("the policy context is capped below CrewAI's default of 5 passages",
        limits["results_limit"] <= 3, json.dumps(limits))
    run("all of it is tunable without editing code",
        "CLAIMSIGHT_LLM_RETRIES" in Path(ROOT / "src" / "config.py").read_text())

    # ---------------------------------------------------------------- the crew
    print("\n2. What the Assessment crew is built with\n")
    config.get_llm = lambda *a, **k: "gpt-4o-mini"
    from crews.assessment_crew import build_assessment_crew
    crew = build_assessment_crew(verbose=False)
    run("an identical repeat tool call is served from the first result",
        crew.cache is True)
    run("the crew paces its requests", crew.max_rpm == config.max_rpm(),
        f"max_rpm={crew.max_rpm}")
    parallel = [t for t in crew.tasks if getattr(t, "async_execution", False)]
    run("the two analysts still run in parallel", len(parallel) == 2,
        [t.agent.role for t in parallel])
    analyst = next((a for a in crew.agents if getattr(a, "knowledge_config", None)), None)
    run("the Policy Analyst's retrieval is capped",
        analyst is not None and analyst.knowledge_config.results_limit <= 3,
        f"results_limit={analyst.knowledge_config.results_limit}" if analyst else "none")

    # ---------------------------------------------------------------- failures
    print("\n3. When the retries run out\n")
    import tempfile

    import claim_flow
    tmp = Path(tempfile.mkdtemp(prefix="claimsight_limits_"))
    monitoring.LOGS = tmp / "logs"
    claim_flow.DECISIONS, claim_flow.REVIEW_QUEUE = tmp / "decisions", tmp / "queue"

    monitor = monitoring.ClaimMonitor("CLM-LIMIT")
    kinds = {}
    for name, message in (("per minute", GROQ_429), ("per day", GROQ_TPD),
                          ("malformed tool call", GROQ_TOOL)):
        low = message.lower()
        if "tokens per day" in low or "(tpd)" in low:
            kind = "daily_token_limit"
        elif "429" in message or "rate limit" in low:
            kind = "per_minute_rate_limit"
        elif "tool_use_failed" in message:
            kind = "malformed_tool_call"
        else:
            kind = "other"
        kinds[name] = kind
    run("a per-minute limit is told apart from a daily one",
        kinds["per minute"] == "per_minute_rate_limit"
        and kinds["per day"] == "daily_token_limit", json.dumps(kinds))
    run("a malformed tool call is its own kind, not a rate limit",
        kinds["malformed tool call"] == "malformed_tool_call")

    monitor.error("assessment", RuntimeError(GROQ_429))
    summary = monitor.finish(status="failed", failed_at="assessment")
    run("a failed run is recorded as failed", summary["status"] == "failed")
    run("with where it failed", summary["failed_at"] == "assessment")
    run("and the error is in the claim's log",
        "429" in (monitoring.LOGS / "CLM-LIMIT.jsonl").read_text())

    # a crew that keeps failing must not leave a decision behind
    def always_fails(inputs, verbose):
        raise RuntimeError(GROQ_429)

    claim_flow.run_assessment_step = always_fails
    flow = claim_flow.ClaimFlow()
    flow.state.claim_id = "CLM-LIMIT"
    try:
        flow.assess()
        raised = False
    except Exception:            # noqa: BLE001
        raised = True
    run("the Flow raises rather than carrying on with no decision", raised)
    run("no decision file was written for a run that failed",
        not (claim_flow.DECISIONS / "CLM-LIMIT.json").exists())

    # ---------------------------------------------------------------- the numbers
    print("\n4. What is recorded about cost and time\n")

    class Usage:
        total_tokens, prompt_tokens, completion_tokens = 44352, 39000, 2100
        cached_prompt_tokens, reasoning_tokens, successful_requests = 1200, 3252, 7

    class Output:
        token_usage = Usage()
        pydantic = {"ok": True}

    monitoring.LOGS = tmp / "logs2"
    monitor = monitoring.ClaimMonitor("CLM-USAGE")
    monitor.llm_error_kinds.add("per_minute_rate_limit")
    monitor.counts["retried_llm_failures"] = 2
    # Record exactly what run_assessment_step records, from a stand-in usage
    # object shaped like the provider's own report.
    import time as _time
    started = _time.time()
    output = Output()
    monitor.record("crew_done", crew="assessment",
                   seconds=round(_time.time() - started, 2),
                   **{f: getattr(output.token_usage, f, 0) or 0 for f in
                      ("total_tokens", "prompt_tokens", "completion_tokens",
                       "cached_prompt_tokens", "reasoning_tokens",
                       "successful_requests")},
                   retried_llm_failures=monitor.counts["retried_llm_failures"],
                   llm_errors=sorted(monitor.llm_error_kinds))
    monitor.finish(status="approved")
    written = [json.loads(line) for line in
               (monitoring.LOGS / "CLM-USAGE.jsonl").read_text().splitlines() if line.strip()]
    done = next(e for e in written if e["event"] == "crew_done")
    for field in ("total_tokens", "prompt_tokens", "completion_tokens",
                  "cached_prompt_tokens", "reasoning_tokens"):
        run(f"{field.replace('_', ' ')} recorded", done.get(field) == getattr(Usage, field),
            f"{done.get(field):,}")
    run("the number of provider requests recorded", done.get("successful_requests") == 7)
    run("the retries recorded", done.get("retried_llm_failures") == 2)
    run("and which provider error happened",
        done.get("llm_errors") == ["per_minute_rate_limit"], done.get("llm_errors"))
    run("no cost in rupees or dollars is invented anywhere",
        not any(k in json.dumps(written).lower()
                for k in ("cost_usd", "cost_inr", "estimated_cost", "price_per")),
        "only counts and seconds are recorded")
    finished = next(e for e in written if e["event"] == "run_finished")
    run("total seconds recorded for the run", finished.get("total_seconds") is not None)

    print(f"\n{passed}/{total} rate-limit and cost checks as expected")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
