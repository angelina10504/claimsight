---
title: ClaimSight A2A
emoji: 🚗
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 9999
pinned: false
---

# ClaimSight

Motor insurance claim adjudication using **CrewAI** and **Google ADK**, connected
through **A2A**, with shared **MCP** tools. Built by Angelina Gupta, Kavya Goswami
and Sahil Dewani for CSE3101 Agentic AI, BML Munjal University.

ClaimSight reads an invoice, policy schedule, claimant narrative and optional
vehicle photographs. It checks completeness, coverage and fraud signals, then
computes a settlement in Python and routes the claim to approval, rejection,
human review or an evidence request.

**Start here:** [architecture](docs/ARCHITECTURE.md) ·
[tests](tests/README.md) · [utilities](scripts/README.md) ·
[deployment](docs/deployment/README.md) ·
[evaluation findings and limitations](docs/CHANGES_SINCE_EVAL.md)

## Repository layout

```text
claimsight/
├── src/                     CrewAI Flow, crews, models, tools and safety checks
│   ├── claim_flow.py        Typed state, execution, routing and evidence resume
│   ├── a2a_server.py        HTTP/A2A interface to the Flow
│   ├── mcp_server.py        Six MCP tools, shared by both frameworks
│   ├── crews/               Intake and assessment agents/tasks
│   └── tools/               Deterministic evidence and settlement functions
├── agents/claimant_assistant/ Google ADK conversational agent
├── dashboard/               Streamlit workspace and isolated evidence sandbox
├── tests/                   Offline checks and replay fixtures
│   └── live/                Model-backed policy retrieval check (costs tokens)
├── scripts/                 Data preparation, diagnostics and offline test runner
├── data/                    Committed synthetic claims, invoices and photo fixtures
├── knowledge/               Canonical policy PDF and extracted wording
├── evaluation/              A/B harness and preserved historical run evidence
├── docs/                    Architecture, findings, deployment and demo evidence
├── run_flow.py              Direct CrewAI command-line entry point
├── run_assistant.py         ADK terminal conversation
├── run_intake.py            Intake-only diagnostic runner
├── run_assessment.py        Assessment-only diagnostic runner
├── run_damage_analyst.py    Photo-check diagnostic runner
├── add_evidence.py          Submit evidence and optionally resume a claim
├── show_report.py           Explain a saved outcome
├── show_log.py              Display a saved claim timeline
├── claimsight_tools         MCP subprocess launcher for CrewAI
└── Dockerfile*              A2A, assistant and dashboard container entry points
```

## Setup

Requires **Python 3.12** and **uv**. Run commands from the repository root.

```bash
uv sync --frozen
cp .env.example .env
# Fill in the required credentials locally; never commit .env.
```

The synthetic dataset and prepared policy wording are already committed. You do
not need to regenerate them to run the application or offline checks.

Text-model configuration defaults to Groq `gpt-oss-120b`; the ADK assistant uses
Gemini. Configure providers and model overrides through the environment. Policy
retrieval defaults to local Ollama (`nomic-embed-text`); set
`CLAIMSIGHT_EMBEDDER=onnx` for in-process CPU embeddings, which download a model
on first use and need no embedding API key.

## Run the application

**These assessment/chat commands call real models and can spend tokens.**

One claim through the CrewAI engine, queuing any human review:

```bash
CLAIMSIGHT_EMBEDDER=onnx uv run python run_flow.py CLM-6001 --no-input
```

Saved intake is reused by default; `--fresh` re-runs intake, `--verbose` shows
agent output, and `--resume` explicitly continues a claim after new evidence.
Omit `--no-input` to answer an adjuster prompt in the terminal.

Both frameworks, in two terminals:

```bash
# Terminal 1: CrewAI engine behind A2A; default port 9999
CLAIMSIGHT_EMBEDDER=onnx uv run python src/a2a_server.py

# Terminal 2: ADK claimant assistant
uv run python run_assistant.py
# Alternative browser chat: uv run adk web agents
```

The assistant discovers the server through
`CLAIMSIGHT_A2A_URL/.well-known/agent-card.json` (base URL defaults to
`http://localhost:9999`). The A2A request identifies an existing claim; the Flow
loads its evidence from the repository's data directories.

Dashboard and saved reports:

```bash
uv run streamlit run dashboard/app.py --server.address 127.0.0.1
CLAIMSIGHT_OFFLINE=1 uv run python show_report.py CLM-6001
uv run python show_log.py CLM-6001
```

Dashboard Presentation mode reads saved results. Local Live assessment starts
`run_flow.py` as a child process and spends tokens. Hosted dashboards are
read-only. Evidence & Recovery uses stand-in crews in its own workspace.

## Run checks without model tokens

```bash
uv run python scripts/run_offline_tests.py
# Or choose checks:
uv run python scripts/run_offline_tests.py test_safety_fixes test_dashboard_readonly
```

The runner clears provider credentials, disables remote trace export, blocks
external sockets in test processes, and leaves loopback available for A2A.
Each test runs in a separate process. Live checks in `tests/live/` are excluded.

`CLAIMSIGHT_OFFLINE=1` alone is not a universal model-call block: it disables
trace export and activates the vision offline path. Use the runner for the
script-based offline suite. See [tests/README.md](tests/README.md) for details.

## Deployment

The recorded cloud deployment has three Cloud Run services: A2A adjudication,
ADK browser chat and a read-only Streamlit dashboard. They share saved records
through Cloud Storage. MCP tools remain stdio child processes within their
respective application containers.

See [deployment notes](docs/deployment/README.md). The root Dockerfiles and
`render.yaml` stay in place for existing build/platform configuration; older
Hugging Face and Render instructions are labelled as historical alternatives.

## Evaluation and known limits

The 14 November evaluation compared a single-agent baseline (A) with the
ClaimSight assessment crew (B). On 20 held-out claims, B completed 17 on 20b;
three remaining claims were completed on 120b as a separately labelled
supplement. The results and original logs remain in `evaluation/runs/`.

**Those results do not measure the current deployed build.** Retrieval, clause
scope checks and safety handling changed afterwards. A later small comparison
found wrong denials, a guardrail-exhaustion failure and an out-of-period claim
approved on a fabricated coverage quote. Current code enforces the policy
period and escalates exhausted assessment guardrails. Positive coverage quotes
remain insufficiently verified; there has been no full re-evaluation.

Read [changes since evaluation](docs/CHANGES_SINCE_EVAL.md) before quoting an
accuracy, payout or timing figure. The dataset is synthetic, its generator
shares a price catalogue with the checks, and protocol tests use stand-in crews;
they do not establish real-world insurance accuracy.

## Documentation

- [Architecture and reading order](docs/ARCHITECTURE.md)
- [Documentation index](docs/README.md)
- [Data and fixture conventions](data/README.md)
- [Offline and live checks](tests/README.md)
- [Data preparation and diagnostics](scripts/README.md)
- [Requirement-to-code map](docs/CODE_MAP.md)
