# Changes since the final evaluation (14 Nov 2026)

The Requirement 10 evaluation (`evaluation/runs/final-evaluation/`) was run on
14 Nov 2026 against commit `0b8f311` and the code before it. It has **not** been
re-run. The system has changed since then, so the deployed system and the
evaluated system are not the same. This file lists every change that can affect
a claim's outcome, so the report can state the drift openly instead of implying
the numbers describe the current build. It is the source for the report's
limitations section.

**What the evaluation measured:**
- Held-out claims CLM-5020 … CLM-5039 on `gpt-oss-20b`.
- Config B completed 17/20 claims. The 3 it could not finish were completed on
  `gpt-oss-120b` as a separate, labelled supplement.
- Policy-clause retrieval used Ollama `nomic-embed-text` with 1200-character
  chunks.
- No clause-applicability check existed.

## Changes that can alter a decision

| # | Change | Commit | Effect on outcomes | Direction |
|---|---|---|---|---|
| 1 | **Clause applicability gate.** `src/clause_scope.py` maps the 51 scoped blocks of the policy wording: 27 that cannot apply to a Private Car Package Policy, 9 add-ons that apply only if purchased, and 15 endorsements that apply only if attached (only IMT 22 is). `citations.check()` now fails a citation to a clause that cannot apply. `decision_guardrail` sends such a decision back through the existing retry. | `45b9384` | Before this, the Policy Analyst could cite a commercial-vehicle endorsement (IMT 21: bumpers and paint work) against a private car, and every check passed. In the cloud this underpaid CLM-6001 by about ₹5,950. A claim that was wrongly cut may now be paid, or sent to a person. | Payout up or human review, never a new denial |
| 2 | **`policy_form` input and Policy Analyst prompt.** The analyst receives `"policy_form": "Private Car Package Policy"` first in the policy schedule. Its backstory gained one sentence saying an IMT endorsement or add-on applies only within the scope named in its own heading. | `45b9384` | Changes what the analyst reads and writes for every claim. In the 18 Nov cloud run of CLM-6001, the analyst reasoned explicitly that IMT 21 applies only to commercial vehicles. (It also wrongly called IMT 21 "the tyre-related exclusion".) | Coverage reasoning |
| 3 | **Narrowed routing rule** (`coverage_doubts`). On a claim found *covered*, a line denied on a scoped clause (an endorsement or add-on with a limited scope) now becomes a doubt and routes to human review. Lines denied on base-policy clauses, prices or duplicates are deliberately **not** escalated, which keeps the rule narrow. Exclusion checks pass the policy record so applicability is tested. | `45b9384` | Some claims that were auto-approved with a line cut would now go to a person. The governing principle, recorded in `clause_scope.py`: over-blocking kills a claim; under-blocking sends it to a human. | More human review, fewer auto-approvals |
| 4 | **Embedder: Ollama `nomic-embed-text` → in-container ONNX `all-MiniLM-L6-v2`** for the deployed server (`CLAIMSIGHT_EMBEDDER=onnx` on Cloud Run). The variable also accepts `google` (Gemini `gemini-embedding-001`, used briefly on Render and dropped because of the free-tier 429s) and `ollama` (the default, used locally and by the offline tests). | `53a4ab1`, `a90d6fe`, `5e7ed68`, `05ef201` | A different embedding model returns different policy passages, so the analyst can be handed different clauses for the same claim. | Retrieval; untested against the evaluation set |
| 5 | **Chunk size 1200 → 6000 characters** (overlap 200 → 300). This applies to every embedder, local runs included, not just the cloud. | `05ef201` | Each retrieved passage is about five times longer, so fewer, broader passages reach the analyst. The policy-knowledge test answer from 18 Nov grades 16/19 (IMT 23 quoted for depreciation; IMT 24 named for painting). | Retrieval; untested against the evaluation set |

Changes 4 and 5 together mean that **the retrieval the evaluation measured
no longer exists anywhere**, local runs included.

## Changes that do not alter a decision

| Change | Commit | Why it does not affect outcomes |
|---|---|---|
| **Registration masking in outbound spans.** `redact_pii(text, outbound=True)` masks Indian vehicle registrations; the span scrubber now scrubs every string attribute (it previously missed `crew_tasks`). | `a8b3c69` | Applies only to text leaving the system (Langfuse spans). The agents still receive the registration, because the invoice-vs-policy registration check needs it. |
| **Monitoring log shared at finish.** `ClaimMonitor.finish()` copies the completed log once to `CLAIMSIGHT_LOG_ARCHIVE` (the bucket), so the dashboard can show agent activity for cloud claims. A failed upload is logged and ignored. | this change | Runs after the outcome is fixed; the log is still written locally event by event. |
| **Read-only dashboard** in the cloud, with live runs disabled. | `5eaee0e` | Display only. |
| **Deployment**: Cloud Run (A2A server, assistant, dashboard) sharing a Cloud Storage bucket; Langfuse host set to the HIPAA region. | `5eaee0e` and Cloud Run config | Infrastructure. Note that Langfuse exported nothing from 28 Oct to 17 Nov because the host was wrong, so **the evaluation run has no traces**. |
| Train annotations untracked from git. | `a63a693` | Repository size only. |

## Not changed

- The text and vision model defaults (`gpt-oss-120b`, `qwen/qwen3.8-27b`) are
  unchanged. The evaluation itself ran on `gpt-oss-20b`, chosen for its larger
  free-tier allowance. The deployment runs `gpt-oss-120b`, the model of the
  3-claim supplement, so the 17-claim headline was measured on a different
  model from the one deployed.
- The settlement calculator, the fraud tools and the other routing thresholds
  are unchanged.
- Retries, rate-limit handling and the vision backoff are unchanged. The 65 s
  vision 429 wait seen in the 18 Nov cloud run is existing behaviour.

## What to say in the report

The evaluation figures describe the 14 Nov build. Since then:
- a clause-applicability gate (1),
- a prompt and input change for the Policy Analyst (2),
- a narrowed escalation rule (3), and
- a different retrieval set-up (4, 5)

have changed how coverage is decided. Changes 1 to 3 can only raise a payout or
send a claim to a person; none of them adds a denial. Changes 4 and 5 change
which clauses the analyst reads, and their effect on accuracy has **not** been
measured. The evaluation was not re-run.

## Confirmatory check: gpt-oss-20b vs gpt-oss-120b (18 Nov 2026)

**Why:** the evaluation's headline numbers come from `gpt-oss-20b`, but the
deployment runs `gpt-oss-120b`. To give this section evidence instead of an
expectation, three development claims were run on both models, plus the demo
claim CLM-6001 on 120b. This is not a re-run of the evaluation. Held-out claims
were not touched. The model defaults were not changed: 20b was selected for
these runs only, through a per-process environment override.

**Setup:**
- **The three claims:** one expected auto-approve (CLM-5001, clean), one expected
  human review (CLM-5014, inflated amount) and one expected reject (CLM-5006,
  incident outside the policy period).
- **No 20b baseline existed for these claims.** The final evaluation used only
  held-out claims, and the October rehearsal used stand-ins with no model. So both
  models were run here, on the same code (commit `c7ae41a`).
- **20b runs:** local, on the same retrieval as the deployment (ONNX embedder,
  6000-character chunks, in an isolated index).
- **120b runs:** the deployed Cloud Run service, called over A2A.
- **Not identical environments.** Local and cloud MCP servers and machines differ,
  so the elapsed times below are indicative only.

| Claim | Expected (ground truth) | 20b route / payout | 120b route / payout | Match |
|---|---|---|---|---|
| CLM-6001 (demo) | approve | not run on 20b | auto-approve / ₹22,611.80 | n/a; same payout as 17 Nov |
| CLM-5001 | approve, full | auto-approve / ₹1,829.05 | auto-approve / ₹1,023.70 | **No:** same route, payout ₹805.35 (44%) lower |
| CLM-5014 | human review | human review (fraud 0.70, overpriced fender denied; proposed ₹36,498.93) | **no decision:** the Flow failed | **No** |
| CLM-5006 | reject (outside period) | reject (policy period) | **auto-approve / ₹22,364.00** | **No:** opposite outcome |

| Claim | 20b tokens / elapsed | 120b tokens / elapsed |
|---|---|---|
| CLM-6001 | n/a | 83,733 / 161.7 s |
| CLM-5001 | 69,268 / 132.2 s | 80,740 / 160.2 s |
| CLM-5014 | 87,785 / 244.2 s | about 105,000 (estimate, see note) / 275.5 s, then failed |
| CLM-5006 | 69,706 / 169.9 s | 79,617 / 179.2 s (assessment only; see note) |

Tokens are the provider's counts from the claim logs.
- **CLM-5014 (120b) estimate:** this run failed before the log recorded its
  assessment total. Langfuse counted 36,678 tokens for it; on the other three
  runs, Langfuse's count was 0.33–0.36 of the logged total, which puts this run
  at about 105,000 (roughly 102,000–111,000).
- **CLM-5006 (120b) retry:** its first attempt failed on infrastructure (the MCP
  tool server timed out after 30 s, before any decision). The retry reused that
  attempt's saved intake, so its figure covers the assessment crew only.

**What it shows:** 120b agreed with 20b on 1 of 3 routes, and on 0 of 3 outcomes
once payouts are counted. Every disagreement traces to the same root cause: the
Policy Analyst and Assessment Lead keep reaching for **IMT 21** (the
commercial-vehicle exclusion for bumpers and paint work). Then the guardrail's
retry loop interacts badly with that:

- **CLM-5001: the IMT 21 denial was relabelled and paid out short.**
  1. The Lead's first decision denied painting under IMT 21. The applicability
     gate stopped it.
  2. Its next decision denied the same line as "Excluded per GENERAL EXCEPTIONS –
     Paint work not covered". The General Exceptions contain no paint-work
     exclusion.
  3. The gate passed it, because the reason names a real heading and quotes no
     text, so there is nothing to test for applicability.

  Result: ₹805.35 underpaid, auto-approved without a person seeing it.
- **CLM-5014: the claim failed instead of going to a person.** The Lead cited
  IMT 21 on all three attempts. The gate correctly refused each one; after two
  retries the task failed and the claim got no decision. 20b also cited IMT 21
  once on this claim, but recovered on its retry.
- **CLM-5006: the retries turned a correct rejection into a wrong approval.**
  The policy-period tool answered correctly ("incident is 37 days BEFORE cover
  began").
  1. The Lead's first decision rejected the claim for that reason, which was
     correct. But it added a second reason citing IMT 21, and the gate rejected
     the whole decision.
  2. Its second decision was "uncertain", which routes to human review. It was
     rejected for having no clause in its coverage reasons.
  3. Its third decision was "covered", citing "Structural repairs and
     replacements are covered (Section I item 5)". That sentence does not exist
     in the policy wording, and the decision also set `is_covered: false`. It
     passed.

  Result: a claim outside the policy period was auto-approved for ₹22,364.00.
  All three of the Lead's outputs were replayed through `decision_guardrail`
  offline (no tokens), and the same verdicts were reproduced.

**What this means for the report:**
1. The evaluation's 20b figures **do not carry over** to the deployed 120b
   configuration. On this small set, 120b was worse, not better, and it failed in
   a way the 20b numbers cannot show.
2. **The gate fixes the IMT 21 symptom but not its cause.** 120b's errors were
   unchanged by it; the guardrail retry loop only changed how they failed.
   Blocking a wrong citation and asking again can push the model to a different
   wrong answer that passes.
3. **Three gaps let the wrong answers through:**
   - A grant-of-cover quote is not checked against the wording.
   - `is_covered` contradicting `coverage_status` is not caught.
   - A failed policy-period tool result is not enforced in Python. The routing
     rule "outside the period → reject" depends on the model reporting it.
4. **These are declared, not fixed.** Engineering stopped here by decision. The
   demo claim CLM-6001 still produces the correct result.

Sample size is four claims, so this shows that the models disagree. It is not a
measure of how often they disagree.

**Artefacts:**
- 120b decision records and logs: the Cloud Storage bucket (`decisions/`, `logs/`).
- 20b decision records and logs: `data/decisions` and `data/logs` (local, untracked).
- Langfuse traces: CLM-6001 `5311949b754513cfdec2f2ee7659088c`, CLM-5001
  `fbf96942626118ff9c89a2f07981d509`, CLM-5014 `4de49aba5a83111d1154378cc3f142cd`,
  CLM-5006 `4c628b6d0afbb8084b6c877123f50d95`.
- 20b traces: CLM-5001 `d3f7a1d53b184ddb3fd7142564b15290`, CLM-5014
  `94baa1333cc3db9a8484f260e23a2229`, CLM-5006 `54d763ef868dfaf3bdefe7f8f8b7445e`.

## Two safety fixes made after the confirmatory check

These fixes change outcomes, so they are drift from the evaluated build as
well. Both are tested offline in `tests/test_safety_fixes.py` (33 checks, no tokens).
The tests replay the real 120b outputs saved in `tests/fixtures/replay/` through
the real `decision_guardrail` and the Flow's own steps.

**A. The policy period is enforced in code, after the crew**
(`enforce_policy_period` in `src/claim_flow.py`).
- **What it does:** `check_policy_period` runs in Python on the decision the
  crew returned. When it says `within_period: false`, the decision becomes
  `excluded`, every line is denied, and the router rejects the claim. The model's
  own verdict and reasons are kept in `period_override` for the record, and the
  override is logged.
- **The date it uses:** the date the claimant **submitted**
  (`data/claims/<id>.json`), not the Intake Officer's model-made copy. A misread
  date can neither excuse an out-of-cover claim nor shut out an in-cover one.
- **When it does nothing:** a tool error (no policy on file, an unreadable date)
  is not treated as "outside the period".
- **Reject, not human review:** the comparison is deterministic arithmetic on
  two dates from files, and the existing router already rejected out-of-period
  claims without a clause. Sending these to a person would only add a step whose
  outcome is fixed.
- **Replayed on CLM-5006:** rejected, ₹0. On 18 Nov this claim was auto-approved
  for ₹22,364.00. Whichever of the three real attempts had passed, the claim is
  rejected.

**B. Guardrail exhaustion escalates to a person; it never fails the claim.**
- **What it does:** when a task's guardrail has refused every retry, the Flow
  records a decision that decides nothing:
  - coverage `uncertain`;
  - lines provisionally admitted, so the adjuster sees what the claim would pay;
  - fraud risk flagged as **not assessed** (`fraud_score` holds 0.0 only because
    the field is numeric).

  The claim is then routed to human review, with the guardrail's last objection
  as the reason.
- **Infrastructure errors still fail as before,** for example the MCP timeout
  seen on CLM-5006's first cloud attempt. Those are worth retrying, not
  escalating.
- **Exhaustion on an out-of-period claim** is rejected by fix A, because no
  model decision is needed.
- **Replayed on CLM-5014:** the claim reaches an adjuster, told that the
  Assessment Lead kept citing IMT 21. On 18 Nov it failed outright.

## Known, unfixed gap: quotes that grant cover are not verified

**The defect:** the citation checks verify the clauses used to **refuse** cover,
meaning that the clause exists, takes cover away, and applies to this policy.
Nothing verifies a clause quoted to **grant** cover. A model can therefore
justify "covered" with text the policy does not contain, and the decision
guardrail accepts it. Fixing this needs a second citation check for positive
claims, and it was deliberately not built. Fix A closes the CLM-5006 case only
because that case also failed the period check. Any in-period claim can still be
granted on an invented quote.

**The evidence:** CLM-5006 on gpt-oss-120b, 18 Nov 2026 (Langfuse trace
`4c628b6d0afbb8084b6c877123f50d95`; all three outputs in
`tests/fixtures/replay/CLM-5006_120b_lead_attempts.json`). The Assessment Lead's
three outputs and what the guardrail did with them:

1. **Attempt 1:** `coverage_status: "excluded"`, `recommended_route: "reject"`.
   Reasons:
   - "Incident outside policy period: incident date 2026-06-05 before policy start
     date 2026-07-12." (correct)
   - The IMT 21 paint-work exclusion, quoted as "Special Exclusions, page 7"
     (cannot apply to a private car).

   Guardrail: **refused**, with "a coverage_reason cites a clause that cannot
   apply: IMT 21 applies only to Commercial Vehicles…". The correct rejection was
   discarded along with the wrong reason.
2. **Attempt 2:** `coverage_status: "uncertain"`, no coverage reasons,
   `recommended_route: "human_review"`.
   Guardrail: **refused**, with "coverage_reasons must say what the policy wording
   showed, with the clause". The safe answer was discarded.
3. **Attempt 3:** `coverage_status: "covered"`, `is_covered: false`,
   `recommended_route: "auto_approve"`. Reason: "\"Structural repairs and
   replacements are covered\" (Section I item 5)".
   Guardrail: **accepted.** That sentence does not occur anywhere in
   `knowledge/policy_wording.txt`. `is_covered: false` contradicts "covered",
   and that contradiction is not checked either.

The claim was then auto-approved for ₹22,364.00. All three verdicts reproduce
offline, without a model.

**Why it matters:** the guardrail's retry loop does not only filter bad answers.
It pushes the model until it produces text the validators accept. Because the
validators check denials but not grants, that pressure points towards granting
cover.
