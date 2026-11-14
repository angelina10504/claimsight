# Requirement 10 — Final Evaluation (held-out test split)

**Run date:** 2026-11-14 (UTC) · **Model:** `openai/openai/gpt-oss-20b` (Groq free tier)
**Split:** `test` — the sealed held-out 40, unlocked with `--final-evaluation`.
**Claims:** first 20 in manifest order, **CLM-5020 … CLM-5039**.
Recorded in `run.json` under `final_evaluation` (used, split, date, exact claim ids).

Command (re-runnable; resumes, never re-spends on finished claims):
```
PYTHONUTF8=1 uv run python evaluation/evaluate_phase2.py --final-evaluation --claims 20 --budget 0
```
Keys: rotated through 7 free Groq accounts via `GROQ_API_KEYS` (200k tokens/key/day).

## Result

| Config | Completed | Notes |
|---|---|---|
| A — single LLM, same evidence | **20 / 20** | |
| B — ClaimSight (Policy+Fraud parallel, tools, guardrails) | **17 / 20** | 3 excluded, see below |
| C — ClaimSight + vision | not available | Phase 3; Damage Analyst not built |

Scoring compares A and B on the **17 claims both finished** (see `results.json`,
`results.csv`, `summary.csv`, and the table printed by `--report`). Headline, this
sample: A correct-decision 88.2% / B 82.3%; both 100% coverage accuracy and 100%
accuracy-when-decided; B better on line-item F1 (0.875 vs 0.714) and payout error
(mean Rs 690.65 vs Rs 1,299.74); A higher fraud precision (100% vs 50%) on a tiny
fraud n. Tokens/claim end-to-end: A ~21.8k, B ~53.5k (intake ~14.2k shared).

## The 3 excluded config-B claims — documented, not hidden

**CLM-5029, CLM-5034, CLM-5038** could not be completed by config B. Config A
assessed all three fine (they are in A's 20/20); only B's multi-agent path failed.

**Cause:** gpt-oss-20b repeatedly emitted **malformed tool-call JSON** for the
Fraud Analyst's `find_prior_claims` tool — Groq returns HTTP 400
`tool_use_failed` ("Failed to parse tool call arguments as JSON" / schema
mismatch), e.g. `{"claim_id":"CLM-10","??"}` with `?` placeholders for
`damaged_parts`. The Flow then produces no decision.

**This is a model limitation, not infrastructure:**
- Reproduced across **4–12 attempts each** (CLM-5029: 12) over four runs.
- Failed on **multiple keys that still had tokens** — not a rate-limit/token issue.
- The embeddings/MCP paths were healthy (A succeeded on the same claims).
- It is intermittent per claim (2 of the original 5 failures cleared on one
  retry: CLM-5030, CLM-5035), but these 3 are deterministic-enough that further
  retries only burn key allowance, so retrying was stopped.

**Decision (owner approved):** report B as 17/20 and document these 3 as a
gpt-oss-20b tool-calling limitation, rather than mix in a different model for 3
claims (which would make config B non-uniform and the 3 non-comparable). The 20b
model was chosen for the evaluation for its larger free-tier daily allowance; its
weaker tool-call reliability vs gpt-oss-120b is itself a finding.

Error evidence is preserved per claim in `B/CLM-5029.error.json`,
`B/CLM-5034.error.json`, `B/CLM-5038.error.json` (`llm_errors: ["malformed_tool_call"]`).

## Supplement — the 3 stragglers completed on gpt-oss-120b

To show the 3 are completable with a stronger model (and that the failure is 20b
tool-calling, not the pipeline), they were re-run on **gpt-oss-120b** in a
**separate** run folder, `evaluation/runs/final-evaluation-120b/`, so the main
20b numbers above stay pure and uniform. Both configs ran on 120b there.

- **All 3 completed first try, zero tool-call errors** (120b handles the
  `find_prior_claims` call reliably). Per-claim model recorded as
  `openai/openai/gpt-oss-120b`.
- Outcomes: CLM-5029 auto_approve (A & B); CLM-5034 and CLM-5038 human_review
  (A & B). On these 3: coverage accuracy 100%, fraud precision/recall 100%,
  line-item F1 1.000, payout error Rs 0.00. (Correct-decision rate reads 33% only
  because 2 of the 3 are escalations by design on an n=3 set.)
- 120b costs more per claim (~38k assessment tokens for B vs ~39k on 20b here,
  but with higher reasoning) and, on the free tier, has its own separate daily
  token quota from 20b.

**Net:** every one of the 20 held-out claims now has a config-B assessment —
17 on gpt-oss-20b (`final-evaluation/`) plus 3 on gpt-oss-120b
(`final-evaluation-120b/`). The headline A-vs-B comparison uses the 17 uniform
20b claims; the 120b set is a labelled supplement, not mixed into those numbers.
