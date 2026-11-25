# Requirement-to-code map

A current navigation index for the eleven project requirements. Use the symbol
names to search the source; the older line-by-line map is preserved in
[the archive](archive/CODE_MAP_PRE_CLEANUP.md), with its stale claims labelled.
For processes and execution paths, start with [architecture](ARCHITECTURE.md).

| # | Requirement | Open | Search for |
|---|---|---|---|
| 1 | State | [claim_flow.py](../src/claim_flow.py) | `ClaimState`, `@persist()`, `ClaimFlow`, `saved_state`, `resume_claim`, `finish` |
| 2 | At least two MCP tools | [mcp_server.py](../src/mcp_server.py), [assessment_crew.py](../src/crews/assessment_crew.py), [ADK agent](../agents/claimant_assistant/agent.py) | Six `@mcp.tool()` functions, `tool_access`, `MCPServerStdio`, `McpToolset`, tool filters |
| 3 | Pydantic structured output | [models.py](../src/models.py), [intake crew](../src/crews/intake_crew.py), [assessment crew](../src/crews/assessment_crew.py) | `ExtractedInvoice`, `ClaimIntake`, `PhotoFindings`, `AssessmentDecision`, `output_pydantic` |
| 4 | Agentic design patterns | [assessment crew](../src/crews/assessment_crew.py), [Flow](../src/claim_flow.py), [evidence.py](../src/evidence.py) | `async_execution=True`, task `context`, knowledge sources, `@router`, `@listen`, `plan`, adjuster gate |
| 5 | Callbacks and logging | [monitoring.py](../src/monitoring.py), [guardrails.py](../src/guardrails.py) | `crew_callbacks`, `on_step`, `on_task_done`, `record_tool_call`, `register_listeners`, `_after_tool_call` |
| 6 | A2A between ADK and CrewAI | [ADK agent](../agents/claimant_assistant/agent.py), [a2a_server.py](../src/a2a_server.py) | `RemoteA2aAgent`, `AgentTool`, `ClaimSightExecutor.execute`, `asyncio.to_thread(run_claim_flow, claim_id)` |
| 7 | CrewAI and ADK only | [pyproject.toml](../pyproject.toml), [crews](../src/crews/), [ADK agent](../agents/claimant_assistant/agent.py) | CrewAI agent/Flow imports, ADK `LlmAgent`; A2A and MCP are protocol libraries |
| 8 | Guardrails | [guardrails.py](../src/guardrails.py), [clause_scope.py](../src/clause_scope.py), [citations.py](../src/citations.py), [Flow](../src/claim_flow.py), [vision.py](../src/vision.py) | `screen_text`, `PrivacyError`, task guardrails, `applicability`, `check`, `enforce_policy_period`, `guardrail_exhausted`, `photo_clearance` |
| 9 | Observability | [observability.py](../src/observability.py), [monitoring.py](../src/monitoring.py) | `start`, `_scrubbing_exporter`, `claim_span`, `archive_log`, context/thread-based `current` |
| 10 | Evaluation, tokens and time | [evaluate_phase2.py](../evaluation/evaluate_phase2.py), [historical runs](../evaluation/runs/), [findings](CHANGES_SINCE_EVAL.md) | Config A/B, `settings_for`, `select_for_run`, `run_unit`, `score`, usage and elapsed-time records |
| 11 | Cloud deployment | [deployment notes](deployment/README.md), [A2A Dockerfile](../Dockerfile), [assistant Dockerfile](../Dockerfile.assistant), [dashboard Dockerfile](../Dockerfile.dashboard) | Three services; `A2A_PUBLIC_URL`, `CLAIMSIGHT_A2A_URL`, shared storage and log archive |

## Six MCP tools

`check_policy_period`, `check_part_prices`, `find_prior_claims`,
`calculate_settlement`, `get_claim_report`, `get_claim_status`.

CrewAI analysts receive filtered subsets. ADK receives status/report tools.
The Flow calls settlement directly in Python; exposing the calculator over MCP
does not mean an agent is assigned that tool. The launcher
[`claimsight_tools`](../claimsight_tools) keeps CrewAI's generated tool names
from colliding with the model's built-in `python` tool.

## Important current corrections

- Cloud deployment is recorded as three Cloud Run services. The root Dockerfiles
  are implemented; external Cloud Run resource and mount settings are not exported
  as infrastructure-as-code here.
- MCP still runs over stdio in child processes. The optional HTTP transport is
  implemented but not required by this deployment arrangement.
- The Damage Analyst exists, conditional on photos. The evaluation's config-C
  unavailability message is stale; C remains absent from the runnable configs.
- Retrieval supports Ollama, Google and ONNX. The current chunk size is 6000,
  overlap 300; the recorded cloud setup uses ONNX.
- Ordinary evaluation stays development-only. `--final-evaluation` is the
  explicit held-out selection path. The dashboard can browse development and
  photo claims; its local live runner accepts development claims only.
- Assessment guardrail exhaustion can produce a code-built uncertain record
  for human review. Infrastructure failures still fail for later retry. Period
  enforcement independently prevents an out-of-cover model approval.
- Denial citation validation does not fully validate invented grants of cover.
  The historical evaluation cannot be quoted as current-system accuracy.

## Supporting paths

- [Offline checks](../tests/README.md) and [real-output replay fixtures](../tests/fixtures/replay/)
- [Live policy retrieval check](../tests/live/test_policy_knowledge.py)
- [Preparation and diagnostic scripts](../scripts/README.md)
- [Demo evidence](demo/) and [generated Flow diagram](flow/claimsight_flow.html)
