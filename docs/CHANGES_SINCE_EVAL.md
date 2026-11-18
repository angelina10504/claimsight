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
