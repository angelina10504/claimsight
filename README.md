# ClaimSight

Multi-agent adjudication of motor insurance claims, built on **CrewAI** and
**Google ADK**, which talk to each other over **A2A** and share one **MCP**
tool server.

A claim arrives as a garage invoice, a policy schedule and a few lines from the
claimant. ClaimSight reads the documents, checks the policy wording, looks for
signs of inflation or duplication, works out what the claim is worth, and then
either approves it, refuses it, asks the claimant for what is missing, or hands
it to a person — saying in each case exactly why.

One rule shapes the whole design: **language models judge, code calculates.**
An agent decides whether a line should be paid. No agent produces a rupee
figure, a route, or a coverage verdict that the code then acts on unchecked.

Course project for CSE3101 Agentic AI. Phase 2.

---

## How a claim moves

```
claimant ──chat──▶ Claimant Assistant            (Google ADK, Gemini)
                        │ A2A
                        ▼
                   ClaimFlow                     (CrewAI Flow, Groq gpt-oss)
                   ├─ Intake crew          Document Reader, Intake Officer
                   ├─ completeness gate    plain Python, overrides the agent
                   ├─ Assessment crew      Policy Analyst ∥ Fraud Analyst
                   │                       then Assessment Lead (Pydantic out)
                   ├─ settlement           Python calculator, no model
                   └─ router               code rules pick the route
                        │                    │
                        └──── MCP ───────────┘
                          six claim tools
```

The router, in order. The first rule that fires decides:

| # | Condition | Route |
|---|---|---|
| 1 | a guardrail fired on the claimant's text | human review |
| 2 | coverage could not be established, or the refusal is not backed by a clause | human review |
| 3 | the payout cannot be computed safely (invoice tax treatment unclear) | human review |
| 4 | the policy excludes it, with a verified clause | reject |
| 5 | fraud score ≥ 0.5 | human review |
| 6 | payout above ₹50,000 | human review |
| 7 | otherwise | auto-approve |

## The six MCP tools

One server, `src/mcp_server.py`, used by the CrewAI agents and by the ADK
assistant. A tool is written once and both frameworks call it.

| Tool | What it answers |
|---|---|
| `check_policy_period` | was the incident inside the cover dates? |
| `check_part_prices` | is any line billed far above the market rate? |
| `find_prior_claims` | has the same damage been claimed before? |
| `calculate_settlement` | what is payable under the depreciation rules |
| `get_claim_status` | where a claim stands |
| `get_claim_report` | why a claim was decided that way, with its evidence |

Every tool validates its input and returns a structured error rather than
raising, so a claim with a missing date is reported as an incomplete file and
never as an exclusion. Launch it through `./claimsight_tools`: CrewAI names
each tool after the launch command, and launching it as `python …` produced
names beginning `python_`, which the model confused with its own built-in
`python` tool.

## Setup

Needs Python 3.12 (CrewAI's dependencies do not build on 3.13+ here) and
[uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env          # then fill in the keys

# local embeddings for the policy RAG
ollama pull nomic-embed-text

# build the dataset and the policy text, once
uv run python generate_data.py
uv run python prepare_policy.py
```

On macOS, WeasyPrint needs its libraries on the path for `generate_data.py`:

```bash
export DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib
```

### Environment variables

Names only; the values go in `.env`, which is never committed. See
`.env.example` for what each one does.

`LLM_PROVIDER`, `GROQ_API_KEY`, `GOOGLE_API_KEY`, `ADK_MODEL`,
`A2A_HOST`, `A2A_PORT`, `CLAIMSIGHT_A2A_URL`,
`CLAIMSIGHT_LLM_RETRIES`, `CLAIMSIGHT_LLM_TIMEOUT`, `CLAIMSIGHT_MAX_RPM`,
`CLAIMSIGHT_KNOWLEDGE_RESULTS`, `CLAIMSIGHT_KNOWLEDGE_THRESHOLD`,
`CLAIMSIGHT_GROQ_MODEL` (default `openai/openai/gpt-oss-20b`; set
`openai/openai/gpt-oss-120b` explicitly to use the larger model),
`LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST` (or
`LANGFUSE_BASE_URL`),
`LANGFUSE_TRACE_CONTENT`, `CLAIMSIGHT_OFFLINE`, `CLAIMSIGHT_TRACING`,
`CREWAI_TRACING_ENABLED`, `OLLAMA_BASE_URL`, `USE_MCP`.

## Running it

```bash
# one claim through the CrewAI side
uv run python run_flow.py CLM-5000
uv run python run_flow.py --demo              # the six-claim demo set
uv run python run_flow.py --plot              # save the Flow diagram

# why a claim was decided that way
uv run python show_report.py CLM-5000
uv run python show_report.py CLM-5000 --markdown
uv run python show_log.py CLM-5000            # the timeline from the callbacks

# both frameworks, end to end  (two terminals)
uv run python src/a2a_server.py
uv run python run_assistant.py --demo
```

### The dashboard

```bash
uv run streamlit run dashboard/app.py --server.address 127.0.0.1
```

A local Streamlit workspace over the same saved results: an overview, a claim
investigation page (assessment, invoice lines, settlement, the explanation from
`src/reporting.py`, the documents), the evidence cycle, and each claim's agent
timeline from its monitoring log. It listens on 127.0.0.1 only.

- **Presentation mode** (the default) reads saved results. It calls no model,
  starts no process and writes nothing, so it works with every quota spent.
- **Live assessment** runs `run_flow.py <claim> --no-input` for one development
  claim, only from its button after a confirmation. It spends Groq tokens and
  replaces that claim's saved result; the old one is copied aside first and can
  be restored if the run fails. It is the CrewAI Flow directly, not the ADK →
  A2A path.
- **Evidence & Recovery** drives the real evidence cycle in its own workspace
  (`dashboard/.runtime/`) with the stand-in crews from `test_evidence_flow.py`,
  including a simulated 429 during the reassessment. No model is called.

Pages can be bookmarked: `?page=Claim+Investigation&claim=CLM-5004`.

### The full ADK → A2A → CrewAI demo, and what it is not

`run_assistant.py` is the real thing: a Gemini-backed ADK agent, the real A2A
protocol, and the real CrewAI crews. It spends Groq and Gemini tokens.

`test_a2a.py` tests the same path with the two crews replaced by stand-ins
built from the dataset's ground truth. It proves the protocol plumbing — the
agent card, the task lifecycle, the artifacts, the guardrail at the boundary —
and proves nothing about the agents' judgement. Both matter, and they are not
interchangeable: a passing `test_a2a.py` does not mean a claim was assessed
well, and a working demo does not mean the protocol handles its edge cases.

## Asking the claimant for what is missing

A claim that stops for want of a document is not failed. It is recorded as
waiting, with the exact item named, and it carries on when that item arrives.

```bash
uv run python test_evidence_flow.py --make-demo-claim   # builds CLM-9002

uv run python run_flow.py CLM-9002 --no-input           # stops, asks for the date
uv run python add_evidence.py CLM-9002                  # what is outstanding
uv run python add_evidence.py CLM-9002 --item "incident date" --value 30-05-2026
#   refused: the date must be YYYY-MM-DD
uv run python add_evidence.py CLM-9002 --item "incident date" --value 2026-05-30 --resume
uv run python show_report.py CLM-9002
```

Steps 1 to 3 call no model at all. Only the reassessment in step 4 costs
tokens, and it re-runs only the assessment: a value the claimant typed is
placed into the saved intake in code, so the documents are not read again.

Evidence is checked by rule, not by a model, because accepting it decides
whether someone is paid. It moves `pending → processing → applied`, and is
only marked applied once a decision has been saved — so a run that fails
leaves the evidence still owed a reassessment rather than losing it. The same
submission sent twice is one piece of evidence: a file is matched on its bytes.

## Reports

`show_report.py` assembles the explanation from the claim's saved state and its
monitoring log. It calls no model: asking an LLM to explain a decision
afterwards produces a plausible story rather than the real reasons.

Each coverage reason is checked against `knowledge/policy_wording.txt` and
labelled with what was actually established:

| Label | Meaning |
|---|---|
| `clause verified` | the clause exists in the policy and supports the conclusion |
| `tool verified` | a date finding that agrees with `check_policy_period` |
| `CLAUSE NOT IN POLICY` | clause-shaped but not in the document |
| `CITES COVER, NOT AN EXCLUSION` | a real clause, listing what the policy covers |
| `NO CLAUSE CITED` | the agent's own words |
| `DISAGREES WITH TOOL` | the agent contradicts what the tool computed |

Money is reported as three different things, because they are three different
things: what the claim is **assessed as worth**, whether a payment is
**authorised**, and what a person actually **authorised**. A claim under review
shows `PENDING HUMAN REVIEW`, never a final ₹0.00, and its questioned lines
read `DISPUTED` rather than `DENIED`.

## Tests

### Fully offline

These call no model and export no trace: Groq and Gemini are never reached,
and the crews are stand-ins where a crew is needed.

```bash
uv run python test_settlement.py        # the calculator against the dataset
uv run python test_audit.py             # the calculator against the policy wording
uv run python test_tools.py             # the deterministic checks, dev split
uv run python test_dataset.py           # dataset isolation and bad input
uv run python test_mcp.py               # the MCP server
uv run python test_guardrails.py        # the four guardrail layers
uv run python test_routing.py           # where a claim goes, and why
uv run python test_evidence_flow.py     # ask, send, check, carry on
uv run python test_report.py            # the explanation and the money
uv run python test_privacy.py           # what actually reaches the model
uv run python test_llm_limits.py        # rate limits, retries, token recording
uv run python test_observability.py     # tracing, in memory
uv run python test_a2a.py               # the A2A protocol, stand-in crews
uv run python test_evaluation.py        # the evaluation harness, stand-in crews
uv run python test_dashboard.py         # the dashboard: pages, data, live guard, sandbox
```

### Calls a real model

```bash
uv run python test_policy_knowledge.py  # the policy RAG: Groq for the answers,
                                        # Ollama for the embeddings; costs tokens
```

### Talks to Langfuse, on purpose

```bash
uv run python check_langfuse.py         # sends ONE synthetic span (no claim
                                        # data, no model call) to check the keys
```

`test_audit.py --table` prints the settlement assumptions with each clause it
rests on.

## Results so far

Offline, and reproducible:

| Check | Result |
|---|---|
| settlement against the dataset | 55/55 within ₹1 |
| settlement against the policy wording | 39/39, all 8 depreciation bands matched |
| deterministic fraud checks, dev split | every rule-catchable fault, 0 false alarms |
| routing | 28/28 |
| evidence cycle | 56/56 |
| reports | 58/58 |
| privacy (payload inspected) | 24/24 |
| rate limits and cost recording | 28/28 |
| tracing (in memory) | 49/49 |
| evaluation harness (stand-in crews) | 55/55 |
| dashboard (AppTest, stand-in crews) | 68/68 |
| MCP | 4/4 tools, 4/4 bad inputs rejected |
| A2A protocol | 5/5 |

With a real model (`test_policy_knowledge.py`): policy questions answered from
the wording, 4/4.

With live models, verified by hand: CLM-5000 auto-approved at ₹41,869.40, and
CLM-5004 routed to human review over an overpriced windshield, with ₹16,081.97
proposed and not yet authorised. Their saved logs record 44,352 tokens and 91
seconds for CLM-5000 (after two 429s), and 39,609 tokens and 49 seconds for
CLM-5004. Those logs predate model recording, so they do not name the model;
the configuration committed at the time hard-coded `gpt-oss-20b`. Runs from
now on record the model in their log.

Accuracy figures come from the **20 development claims**. The other **40 are
held out** for the final evaluation and are not used for prompt tuning or for
any accuracy claim in Phase 2.

## What is Phase 2, and what is not

Done in Phase 2: shared Flow state, six tools over MCP, Pydantic structured
output, several agentic design patterns, callback logging, A2A between ADK and
CrewAI, guardrails at every boundary, Langfuse tracing, the evidence cycle, the
evidence-backed report, policy-citation verification and the auditable
settlement.

Phase 3: the end-to-end evaluation over the 40 held-out claims with cost and
time; cloud deployment; a vision Damage Analyst for photographs; a separately
generated test set with unseen prices and garage formats.

## Known limitations

- **Phantom parts are not caught.** A part billed but never damaged looks
  normal on paper. Only a photograph shows it, which is the Phase-3 vision
  work. The deterministic checks catch inflated prices, out-of-period
  incidents and duplicates; wear-and-tear exclusions and phantom parts are
  left to the agents, and the phantom case is currently missed.
- **The figures come from synthetic data our own generator produced**, and the
  generator and the price-check tool share one price catalogue. That makes the
  fraud results optimistic. An honest number needs a separately generated test
  set, which is Phase-3 work.
- **GST, the tax treatment of invoice amounts, the Zero Depreciation add-on and
  rounding are our model's assumptions, not clauses from the policy wording.**
  `test_audit.py` prints which rules are quoted from the policy and which are
  ours.
- **Free-tier rate limits shape what can be measured.** Groq's free tier allows
  200,000 tokens a day and limits tokens per minute, and a claim costs roughly
  40,000 tokens, so a full evaluation has to be spread over several days.
- **Remote Langfuse export needs verifying on the machine that runs it**, with
  `check_langfuse.py`. A key issued for one region is refused by another.
- **Tracing is verified locally, not as one live distributed trace.** In one
  process, ADK and CrewAI spans share a trace (`test_observability.py`, in
  memory), and remote export is verified with a single synthetic span. The live
  demo runs ADK and CrewAI in two processes and passes no trace context over
  A2A, so a live claim appears in Langfuse as two traces, not one.
- **Monitoring keeps one active claim at a time** (`src/monitoring.py`). That
  is fine for the sequential demos; concurrent claims would mix their events,
  so per-request context is needed before any deployment.
- **The policy RAG needs Ollama running locally**, which a free cloud host will
  not have. Cloud deployment needs a hosted embedder or a prebuilt index.
