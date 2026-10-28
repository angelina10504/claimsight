# ClaimSight code map

Which line implements what. Every entry is `file:line` (or `file:start-end`),
the line of code quoted exactly as it appears in the file, and what it does.
Paths are from the project root. Line numbers were read from the files at the
time this map was written; if a file is edited afterwards they can move.

## Contents

1. [State](#1-state)
2. [MCP: at least two tools](#2-mcp-at-least-two-tools)
3. [Pydantic structured output](#3-pydantic-structured-output)
4. [Agentic design patterns](#4-agentic-design-patterns)
5. [Callbacks and logging](#5-callbacks-and-logging)
6. [A2A between Google ADK and CrewAI](#6-a2a-between-google-adk-and-crewai)
7. [CrewAI and ADK only](#7-crewai-and-adk-only)
8. [Guardrails](#8-guardrails)
9. [Observability](#9-observability)
10. [Evaluation](#10-evaluation)
11. [Cloud deployment](#11-cloud-deployment)
- [A. The Damage Analyst and the photograph](#a-the-damage-analyst-and-the-photograph)
- [B. "LLMs judge, code calculates"](#b-llms-judge-code-calculates)
- [C. Policy citation verification](#c-policy-citation-verification)
- [D. The dataset split](#d-the-dataset-split)
- [E. How the agents are stopped from seeing the answer](#e-how-the-agents-are-stopped-from-seeing-the-answer)
- [If he asks for one line](#if-he-asks-for-one-line)

---

## 1. State

| Item | Where | What it does |
|---|---|---|
| The state model | `src/claim_flow.py:61-80`<br>`class ClaimState(BaseModel):` | The Flow's typed state: one Pydantic object that every step reads and writes, so data moves between steps as named fields, not loose strings. |
| What the crews produced | `src/claim_flow.py:66-68`<br>`intake: Dict = Field(default_factory=dict)` | `intake`, `decision` and `settlement` hold the Intake crew's record, the Assessment Lead's decision and the calculator's result. |
| Where the claim ended up | `src/claim_flow.py:69-71`<br>`route: str = ""` | `route`, `status` and `final_payout`: what the routers decided and what was authorised. |
| Doubts the router acts on | `src/claim_flow.py:72-75`<br>`guardrail_flags: List[str] = Field(default_factory=list)` | Guardrail flags, coverage doubts, evidence applied and payout doubts: each is a reason code found for sending the claim to a person. |
| The photo check | `src/claim_flow.py:77-78`<br>`photo_check: Dict = Field(default_factory=dict)` | The photograph-against-invoice comparison and the doubts it raised, saved in the state so they are part of the decision record. |
| Resumed flag and trail | `src/claim_flow.py:79-80`<br>`resumed: bool = False` | Marks a run that continued a waiting claim; `trail` (line 80) is the human-readable history of every step. |
| The trail is written | `src/claim_flow.py:424`<br>`self.state.trail.append(message)` | Every `self.log(...)` call appends to the state's trail as well as the log file. |
| @persist is applied | `src/claim_flow.py:420-421`<br>`@persist()` | CrewAI's `@persist` checkpoints the state after every step, on the class `ClaimFlow(Flow[ClaimState])`. |
| Why @persist is there | `src/claim_flow.py:17`<br>`@persist saves the flow's state after every step (checkpointing), so each` | The module docstring's own statement of what persist is for. |
| State written: the intake | `src/claim_flow.py:476`<br>`evidence.write_json(saved, self.state.intake)` | After the Intake crew runs, its output is saved to `data/intake/<claim>.json`, so later runs can reuse it. |
| State written: queued for a person | `src/claim_flow.py:632`<br>`evidence.write_json(REVIEW_QUEUE / f"{self.state.claim_id}.json",` | A claim waiting for an adjuster is written to `data/review_queue/`. |
| State written: the decision | `src/claim_flow.py:656`<br>`evidence.write_json(DECISIONS / f"{self.state.claim_id}.json",` | `finish()` writes the whole state to `data/decisions/<claim>.json` at the end of every path. |
| Saved state read back | `src/claim_flow.py:316`<br>`def saved_state(claim_id: str) -> dict:` | Reads the last saved state from the decisions or review-queue folder. |
| Intake reused, not re-run | `src/claim_flow.py:464-465`<br>`if self.state.use_saved_intake and saved.exists():` | If a saved intake exists, it is loaded instead of running the Intake crew again. |
| Resume entry point | `src/claim_flow.py:325`<br>`def resume_claim(claim_id: str, interactive: bool = True, verbose: bool = False) -> dict:` | Continues a claim that was waiting for evidence. |
| Decide what to re-run | `src/claim_flow.py:353`<br>`steps = evidence.plan(state, items)` | Asks the evidence planner which crews the new evidence affects. |
| Patch the intake in code | `src/claim_flow.py:358`<br>`intake, changes = evidence.apply_to_intake(intake, items)` | A missing value (a date, a total) is written into the saved intake by code, so the documents are not read again. |
| Kick off without intake | `src/claim_flow.py:376`<br>`"use_saved_intake": not steps["run_intake"],` | The resumed Flow reuses the saved intake unless a new invoice arrived. |
| The planner's rule | `src/evidence.py:385`<br>`if item.get("kind") == "file" and _norm("invoice") in _norm(item["item"]):` | Only a new invoice document forces the Intake crew to run again. |

SAY: "This is the Flow's state, a Pydantic model, and `@persist` on line 420 checkpoints it after every step; on resume, line 376 reuses the saved intake instead of re-reading the documents."

---

## 2. MCP: at least two tools

**Six tools on the server.** Count the `@mcp.tool()` lines: 98, 108, 118, 129, 144, 174.

| Item | Where | What it does |
|---|---|---|
| FastMCP imported | `src/mcp_server.py:35`<br>`from mcp.server.fastmcp import FastMCP   # noqa: E402` | The official MCP Python SDK's server class. |
| Server constructed | `src/mcp_server.py:52`<br>`mcp = FastMCP("claimsight-tools", port=8765, log_level="WARNING")` | One MCP server, shared by both frameworks. |
| Tool 1 | `src/mcp_server.py:98-99`<br>`def check_policy_period(policy_no: str, incident_date: str) -> dict:` | Was the incident inside the cover dates? Used by the Policy Analyst. |
| Tool 2 | `src/mcp_server.py:108-109`<br>`def check_part_prices(line_items: List[PriceLine]) -> dict:` | Is any billed part far above the market price? Used by the Fraud Analyst. |
| Tool 3 | `src/mcp_server.py:118-119`<br>`def find_prior_claims(policy_no: str, claim_id: str, incident_date: str,` | Has the same damage been claimed before on this policy? Used by the Fraud Analyst. |
| Tool 4 | `src/mcp_server.py:129-130`<br>`def calculate_settlement(policy_no: str, line_items: List[SettlementLine]) -> dict:` | The payout calculator, exposed over MCP. No agent is currently given it: the Flow calls the same calculator directly in Python (see B). |
| Tool 5 | `src/mcp_server.py:144-145`<br>`def get_claim_report(claim_id: str) -> dict:` | The evidence report for a decided claim, PII-redacted (line 168). Used by the ADK assistant. |
| Tool 6 | `src/mcp_server.py:174-175`<br>`def get_claim_status(claim_id: str) -> dict:` | Where a claim stands. Used by the ADK assistant. |
| Server started | `src/mcp_server.py:196`<br>`mcp.run(transport="streamable-http" if "--http" in sys.argv else "stdio")` | Runs over stdio when a client launches it; `--http` serves it over HTTP instead. |
| CrewAI: client import | `src/crews/assessment_crew.py:33`<br>`from crewai.mcp import MCPServerStdio, create_static_tool_filter` | CrewAI's MCP client. |
| CrewAI: connects to the server | `src/crews/assessment_crew.py:64-69`<br>`server = MCPServerStdio(` | Launches the MCP server as a subprocess and returns it as `{"mcps": [server]}` for an Agent. |
| CrewAI: through the launcher | `src/crews/assessment_crew.py:65`<br>`command="./claimsight_tools",` | The CrewAI side starts the server through the launcher script. |
| CrewAI: each agent sees only its tools | `src/crews/assessment_crew.py:67`<br>`tool_filter=create_static_tool_filter(allowed_tool_names=list(names)),` | A per-agent allow-list of tool names. |
| CrewAI: Policy Analyst's tools | `src/crews/assessment_crew.py:96`<br>`**tool_access("check_policy_period"),` | One tool. |
| CrewAI: Fraud Analyst's tools | `src/crews/assessment_crew.py:114`<br>`**tool_access("check_part_prices", "find_prior_claims"),` | Two tools. |
| CrewAI: MCP can be switched off | `src/crews/assessment_crew.py:52`<br>`USE_MCP = os.getenv("USE_MCP", "true").lower() != "false"` | With `USE_MCP=false` the same functions are given as direct CrewAI tools; MCP is the default. |
| ADK: client imports | `agents/claimant_assistant/agent.py:33-34`<br>`from google.adk.tools.mcp_tool.mcp_toolset import McpToolset              # noqa: E402` | ADK's MCP client. |
| ADK: attaches the same server | `agents/claimant_assistant/agent.py:113-115`<br>`claim_status = McpToolset(` | Launches `src/mcp_server.py`, the same server file CrewAI uses. |
| ADK: launched with python directly | `agents/claimant_assistant/agent.py:115`<br>`server_params=StdioServerParameters(command="python", args=[str(ROOT / "src" / "mcp_server.py")]),` | ADK does not use the launcher: the naming problem it fixes is CrewAI's, not ADK's. |
| ADK: its tools | `agents/claimant_assistant/agent.py:118`<br>`tool_filter=["get_claim_status", "get_claim_report"],` | The assistant sees only the two read-only tools. |
| ADK: tools given to the agent | `agents/claimant_assistant/agent.py:161`<br>`tools=[AgentTool(agent=adjudicator), claim_status],` | The MCP toolset sits beside the A2A adjudicator. |
| The launcher script | `claimsight_tools:9`<br>`cd "$(dirname "$0")" && exec python src/mcp_server.py "$@"` | Starts the server from the project root. |
| Why the launcher exists | `claimsight_tools:4`<br>`# Why a launcher: CrewAI names each MCP tool "<launch command>_<tool>". Launched` | Launched as `python ...`, CrewAI named the tools `python_src_mcp_server_py_...`. gpt-oss has a built-in tool called `python`, sometimes cut the name short and called `python`, and Groq rejected the call. Through the launcher the tools are `claimsight_tools_check_part_prices` and so on. |

SAY: "One FastMCP server with six `@mcp.tool()` functions; CrewAI attaches it on line 64 of the assessment crew, ADK attaches the same file on line 113 of the assistant, and each agent sees only the tools its filter allows."

---

## 3. Pydantic structured output

| Item | Where | What it does |
|---|---|---|
| LineItem | `src/models.py:14`<br>`class LineItem(BaseModel):` | One invoice line: description, amount, material. Part of ExtractedInvoice. |
| ExtractedInvoice | `src/models.py:24`<br>`class ExtractedInvoice(BaseModel):` | Document Reader → Claims Intake Officer: the invoice as structured data. |
| PhotoDamage | `src/models.py:35`<br>`class PhotoDamage(BaseModel):` | One photograph's verdicts: damaged, undamaged, not_visible, damage types, severity. |
| severity is constrained | `src/models.py:55`<br>`severity: Literal["minor", "moderate", "severe"] = "minor"` | A Literal, so any other word fails validation. |
| PhotoFindings | `src/models.py:60`<br>`class PhotoFindings(BaseModel):` | Damage Analyst → `run_intake`: one PhotoDamage per photo. |
| ClaimIntake | `src/models.py:66`<br>`class ClaimIntake(BaseModel):` | Intake crew → the Flow (`state.intake`) → Assessment crew inputs. |
| Photo findings in the intake | `src/models.py:80`<br>`photo_damage: List[PhotoDamage] = Field(` | Kept apart from `damaged_parts` (line 74), which comes from the invoice. |
| LineDecision | `src/models.py:98`<br>`class LineDecision(BaseModel):` | One line admitted or not, with a reason. Part of AssessmentDecision. |
| AssessmentDecision | `src/models.py:109`<br>`class AssessmentDecision(BaseModel):` | Assessment Lead → the Flow (`state.decision`) → settlement calculator and router. Has no payout field on purpose. |
| Three-way coverage | `src/models.py:114`<br>`coverage_status: Literal["covered", "excluded", "uncertain"] = Field(` | "uncertain" exists so the model never has to guess between covered and excluded. |
| EvidenceReference | `src/models.py:164`<br>`class EvidenceReference(BaseModel):` | One piece of evidence in a report, and where it came from. Built by `src/reporting.py`. |
| EvidenceRequest | `src/models.py:180`<br>`class EvidenceRequest(BaseModel):` | Something still missing and who must supply it. Built by `src/evidence.py:65`. |
| ClaimReport | `src/models.py:188`<br>`class ClaimReport(BaseModel):` | The explainable report, assembled in code by `src/reporting.py:359`, not by a model. |
| The Flow's state | `src/claim_flow.py:61`<br>`class ClaimState(BaseModel):` | Also Pydantic (see 1). |
| Task output: invoice | `src/crews/intake_crew.py:183`<br>`output_pydantic=ExtractedInvoice,` | The Document Reader must return an ExtractedInvoice. |
| Task output: intake | `src/crews/intake_crew.py:212`<br>`output_pydantic=ClaimIntake,` | The Claims Intake Officer must return a ClaimIntake. |
| Task output: photos | `src/crews/intake_crew.py:253`<br>`output_pydantic=PhotoFindings,` | The Damage Analyst must return PhotoFindings. |
| Task output: decision | `src/crews/assessment_crew.py:225`<br>`output_pydantic=AssessmentDecision,` | The Assessment Lead must return an AssessmentDecision. |
| Hand-off: intake to the Flow | `src/crews/intake_crew.py:303`<br>`intake = result.tasks_output[1].pydantic` | The validated ClaimIntake object is taken from the crew's second task. |
| Hand-off: decision to the Flow | `src/claim_flow.py:110-112`<br>`if output.pydantic is None:` | No valid AssessmentDecision means the run stops with an error; nothing unvalidated reaches the router. |
| The one validator | `src/models.py:150-151`<br>`@model_validator(mode="after")` | `_derive_is_covered`: the only `@model_validator` in the project. There is no `@field_validator`. |
| What it enforces | `src/models.py:155`<br>`object.__setattr__(self, "is_covered", self.coverage_status == "covered")` | `is_covered` is never taken from the model: it is derived, so "uncertain" can never pay out as if it were "covered". |

SAY: "Every hand-off between agents is a Pydantic model enforced with `output_pydantic`, and the one validator on line 150 makes sure an uncertain coverage verdict can never be read as covered."

---

## 4. Agentic design patterns

| Pattern | Where | What it does |
|---|---|---|
| Parallel agents | `src/crews/assessment_crew.py:153`<br>`async_execution=True,` | The Policy Analyst's task runs asynchronously... |
| Parallel agents | `src/crews/assessment_crew.py:179`<br>`async_execution=True,` | ...at the same time as the Fraud Analyst's. |
| Join after the parallel step | `src/crews/assessment_crew.py:224`<br>`context=[coverage_task, fraud_task],` | The Assessment Lead waits for both reports and gets them as context. |
| Router 1 (completeness) | `src/claim_flow.py:509`<br>`@router(intake, emit=["complete", "incomplete"])` | Branches to assessment or to an evidence request. |
| Router 2 (route) | `src/claim_flow.py:573`<br>`@router(assess, emit=["auto_approve", "human_review", "reject"])` | Branches to approve, a person, or reject. |
| The branches | `src/claim_flow.py:517`<br>`@listen("incomplete")` | `@listen` methods are the router targets: lines 517, 536, 587, 594 and 602. |
| RAG: the knowledge source | `src/crews/assessment_crew.py:79-80`<br>`policy_wording = TextFileKnowledgeSource(` | The real policy wording (`knowledge/policy_wording.txt`), chunked and embedded. |
| RAG: embeddings | `src/crews/assessment_crew.py:50`<br>`EMBEDDER = {"provider": "ollama", "config": {"model_name": "nomic-embed-text"}}` | Local embeddings through Ollama. |
| RAG: the agent that uses it | `src/crews/assessment_crew.py:97`<br>`knowledge_sources=[policy_wording],` | Given to the Policy Analyst only. |
| Human in the loop: the route | `src/claim_flow.py:602`<br>`@listen("human_review")` | `send_to_adjuster`: the claim stops for a person. |
| Human in the loop: where it stops (demo) | `src/claim_flow.py:631-634`<br>`if not self.state.interactive:` | Non-interactive: the state is queued in `data/review_queue/` and the status becomes `awaiting_review`. Nothing is paid. |
| Human in the loop: where it stops (terminal) | `src/claim_flow.py:639`<br>`answer = input("\nAdjuster: [a]pprove, [r]eject, or type an amount to approve: ").strip().lower()` | Interactive: the Flow blocks until a person answers. |
| Evidence loop: ask | `src/claim_flow.py:517-534`<br>`def request_evidence(self):` | Names each missing item and how to send it, then finishes with `awaiting_evidence`. |
| Evidence loop: what is owed | `src/evidence.py:65`<br>`def outstanding(state: dict) -> List[EvidenceRequest]:` | The exact items still missing, minus anything already accepted (line 73). |
| Evidence loop: validate | `src/evidence.py:282`<br>`ok, kept, why = checker(value)` | Each value is checked by a plain rule (date, amount, plate); files by `check_file` (line 266). |
| Evidence loop: duplicates | `src/evidence.py:307`<br>`if existing.get("fingerprint") == entry["fingerprint"] and existing.get("accepted"):` | The same evidence sent twice is recorded once. |
| Evidence loop: resume | `src/claim_flow.py:325`<br>`def resume_claim(claim_id: str, interactive: bool = True, verbose: bool = False) -> dict:` | Re-runs only what the evidence affects (see 1). |
| Evidence loop: nothing lost on failure | `src/evidence.py:336`<br>`def mark_processing(claim_id: str, entries: List[dict]) -> None:` | Evidence is marked processing before the run and applied (line 343) only after a decision is saved. |
| Deterministic tool | `src/tools/assessment_tools.py:78`<br>`def check_policy_period(policy_no: str, incident_date: str) -> dict:` | Plain Python date arithmetic behind the MCP tool. |
| Deterministic tool | `src/tools/assessment_tools.py:113`<br>`def check_part_prices(line_items: List[dict]) -> dict:` | Compares each line with a fixed market price table. |
| Deterministic tool | `src/tools/assessment_tools.py:156`<br>`def find_prior_claims(policy_no: str, claim_id: str, incident_date: str,` | Looks up earlier claims on the same policy. |
| Other: reflection / self-correction | `src/crews/assessment_crew.py:154-155`<br>`guardrail=guardrails.coverage_report_guardrail,` | A failing report is sent back to the agent with the reason and retried, up to `guardrail_max_retries=2`. |
| Other: checkpointing | `src/claim_flow.py:420`<br>`@persist()` | The state is saved after every step (see 1). |
| Other: agent as a tool | `agents/claimant_assistant/agent.py:161`<br>`tools=[AgentTool(agent=adjudicator), claim_status],` | The ADK assistant calls the whole remote CrewAI system as one tool. |
| Other: planning | `src/evidence.py:376`<br>`def plan(state: dict, items: List[dict]) -> dict:` | Works out which crews must run again before resuming. |
| Other: retry with backoff | `src/vision.py:276`<br>`for attempt in range(VISION_ATTEMPTS):` | The vision call waits out a per-minute rate limit and retries (3 attempts, line 57). |
| Other: retry on rate limits | `src/config.py:76`<br>`kwargs["max_retries"] = int(os.getenv("CLAIMSIGHT_LLM_RETRIES", "5"))` | Text-model calls retry a 429 using the provider's Retry-After. |

SAY: "The two analysts run in parallel on lines 153 and 179, the Policy Analyst uses RAG over the real policy wording on line 97, and two `@router`s on lines 509 and 573 send a claim to evidence, a person, approval or rejection."

---

## 5. Callbacks and logging

| Item | Where | What it does |
|---|---|---|
| Callbacks for a Crew | `src/monitoring.py:216-220`<br>`def crew_callbacks(monitor: Optional[ClaimMonitor]) -> dict:` | Returns `{"step_callback": on_step, "task_callback": on_task_done}` for `Crew(...)`. |
| Attached: intake crew | `src/crews/intake_crew.py:265`<br>`**crew_callbacks(monitor),     # step and task callbacks -> data/logs/<claim>.jsonl` | |
| Attached: assessment crew | `src/crews/assessment_crew.py:239`<br>`**crew_callbacks(monitor),     # step and task callbacks -> data/logs/<claim>.jsonl` | |
| Step callback | `src/monitoring.py:150`<br>`def step_callback(self, step: Any) -> None:` | After each agent step: a tool call, or an agent's answer. |
| Task callback | `src/monitoring.py:164`<br>`def task_callback(self, output: Any) -> None:` | When a task finishes: agent, task, seconds, structured type, output. |
| Event bus listener | `src/monitoring.py:240`<br>`@crewai_event_bus.on(LLMCallFailedEvent)` | Records a failed model call (e.g. a 429) that CrewAI retries. |
| CrewAI hook: tool calls | `src/guardrails.py:426`<br>`register_after_tool_call_hook(_after_tool_call)` | A global hook that fires for every tool, MCP ones included. |
| CrewAI hook: before each LLM call | `src/guardrails.py:427`<br>`register_before_llm_call_hook(_before_llm_call)` | Masks PII in every outgoing message. |
| ADK: before the model | `agents/claimant_assistant/agent.py:163`<br>`before_model_callback=screen_claimant_message,` | Screens each claimant message (function at line 52). |
| ADK: after a tool | `agents/claimant_assistant/agent.py:164`<br>`after_tool_callback=check_tool_result,` | Type-checks every tool result (function at line 93). |
| Where a tool call is written | `src/guardrails.py:381`<br>`monitor.record_tool_call(` | The after-tool-call hook writes the call to the log, because the step callback sees nothing when the model uses native tool calling. |
| The one writer | `src/monitoring.py:120`<br>`def record_tool_call(self, tool: str, tool_input: Any, result: Any,` | Every tool call goes through this method. |
| How a duplicate is avoided | `src/monitoring.py:138-140`<br>`key = (_name_key(tool), _short(tool_input))` | The hook and the step callback can both report one call. The key ignores punctuation in the name and ignores the result, and a key already seen returns early. |
| Written to the log | `src/monitoring.py:145`<br>`self.record("tool_call", tool=tool, input=_short(tool_input),` | One JSON line per tool call. |
| The log folder | `src/monitoring.py:36`<br>`LOGS = ROOT / "data" / "logs"` | |
| The log file | `src/monitoring.py:93`<br>`self.path = LOGS / f"{claim_id}.jsonl"` | Each claim writes `data/logs/<claim_id>.jsonl`. |
| A log per run | `src/claim_flow.py:460`<br>`monitoring.ClaimMonitor(self.state.claim_id, provider=config.PROVIDER,` | The Flow opens a fresh log at the start of every run, with the provider and model recorded. |

SAY: "Both crews get the same step and task callbacks on lines 265 and 239, every tool call is written once through `record_tool_call` on line 120, and the result is `data/logs/<claim>.jsonl`."

---

## 6. A2A between Google ADK and CrewAI

| Item | Where | What it does |
|---|---|---|
| Server: the executor | `src/a2a_server.py:89`<br>`class ClaimSightExecutor(AgentExecutor):` | The A2A executor that wraps the CrewAI Flow. |
| Server: handles a request | `src/a2a_server.py:92`<br>`async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:` | Reads a claim id from the message, runs the Flow, returns the decision as artifacts. |
| **The crossing** | `src/a2a_server.py:123`<br>`state = await asyncio.to_thread(run_claim_flow, claim_id)   # the Flow runs its own loop` | The line where a task sent by the ADK agent over A2A starts the CrewAI Flow. |
| ...which calls the Flow | `src/a2a_server.py:61-62`<br>`from claim_flow import run_or_resume` | `run_claim_flow` hands over to `run_or_resume`: a new claim, or a waiting claim with new evidence. |
| Server: the AgentCard | `src/a2a_server.py:169-171`<br>`def agent_card() -> AgentCard:` | Named `claimsight_adjudicator`. |
| Server: its one skill | `src/a2a_server.py:181-182`<br>`id="adjudicate_claim", name="Adjudicate a motor insurance claim",` | Declares the `adjudicate_claim` skill, with examples such as "Please assess claim CLM-5004". |
| Server: JSON-RPC binding | `src/a2a_server.py:176`<br>`supported_interfaces=[AgentInterface(url=PUBLIC_URL, protocol_binding="JSONRPC",` | The card advertises a JSON-RPC interface at the public URL. |
| Server: routes mounted | `src/a2a_server.py:196`<br>`app.routes.extend([*create_agent_card_routes(card), *create_jsonrpc_routes(handler, "/", enable_v0_3_compat=True)])` | The agent-card route and the JSON-RPC route on a Starlette app. |
| Server: the port | `src/a2a_server.py:49`<br>`PORT = int(os.getenv("A2A_PORT", "9999"))` | 9999 by default. |
| Server: started | `src/a2a_server.py:205`<br>`uvicorn.run(app, host=HOST, port=PORT, log_level="warning")` | |
| Client: import | `agents/claimant_assistant/agent.py:30`<br>`from google.adk.agents.remote_a2a_agent import RemoteA2aAgent             # noqa: E402` | |
| Client: the remote agent | `agents/claimant_assistant/agent.py:106-110`<br>`adjudicator = RemoteA2aAgent(` | The ADK side declares the CrewAI system as a remote agent... |
| Client: found by its card | `agents/claimant_assistant/agent.py:110`<br>`agent_card=f"{A2A_URL}/.well-known/agent-card.json",` | ...located through the AgentCard the server publishes. |
| Client: used as a tool | `agents/claimant_assistant/agent.py:161`<br>`tools=[AgentTool(agent=adjudicator), claim_status],` | The assistant calls it like any tool. |

SAY: "The ADK assistant declares the CrewAI system as a `RemoteA2aAgent` on line 106, finds it through the AgentCard on line 110, and line 123 of the A2A server is where that request starts the CrewAI Flow."

---

## 7. CrewAI and ADK only

| Item | Where | What it does |
|---|---|---|
| CrewAI imported (intake) | `src/crews/intake_crew.py:36`<br>`from crewai import Agent, Crew, Process, Task` | |
| CrewAI imported (assessment) | `src/crews/assessment_crew.py:31`<br>`from crewai import Agent, Crew, Process, Task` | |
| ADK imported | `agents/claimant_assistant/agent.py:29`<br>`from google.adk.agents import LlmAgent                                    # noqa: E402` | The assistant is an ADK `LlmAgent`. |
| Dependency: CrewAI | `pyproject.toml:9`<br>`"crewai[litellm]>=1.15.22",` | |
| Dependency: ADK | `pyproject.toml:18`<br>`"google-adk[a2a]>=2.13.0",` | |
| No other framework | `pyproject.toml:7-24`<br>`dependencies = [` | The full dependency list is chromadb, crewai, crewai-tools, faker, pdfplumber, pydantic, python-dotenv, streamlit, weasyprint, ollama, google-adk, langfuse, two OpenInference instrumentors and OpenTelemetry. None is an agent framework: there is no LangChain, LangGraph, AutoGen, LlamaIndex or Semantic Kernel, and a grep of the code for their imports finds nothing. |

SAY: "Line 9 and line 18 of `pyproject.toml` are the only two agent frameworks; everything else is storage, documents, tracing or the dashboard."

---

## 8. Guardrails

| Item | Where | What it does |
|---|---|---|
| Input screening | `src/guardrails.py:61`<br>`def screen_text(text: str) -> dict:` | Classifies claimant text as clean, abusive or injection (injection wins). |
| ...in the Flow | `src/claim_flow.py:444`<br>`result = guardrails.screen_text(claim.get("narrative", ""))` | Runs before any agent reads the narrative. |
| ...forces a person | `src/claim_flow.py:449`<br>`f"possible prompt injection in claimant text: {result['injection_matches'][:2]}")` | An injection becomes a guardrail flag, and `decide_route` sends any guardrail flag to human review (line 278). |
| ...in the intake crew | `src/crews/intake_crew.py:87`<br>`submission["narrative"], _ = guardrails.safe_narrative(submission.get("narrative", ""))` | The narrative given to the Intake crew is the screened one. |
| ...in the ADK assistant | `agents/claimant_assistant/agent.py:52`<br>`def screen_claimant_message(callback_context, llm_request):` | Injection never reaches Gemini; abusive words are masked. |
| PII: personal fields dropped | `src/crews/intake_crew.py:94`<br>`if k not in PII_FIELDS}, indent=2)` | Name, phone and address are removed from the policy schedule before an agent sees it (`PII_FIELDS` at line 58). |
| PII: invoice cleaned or stopped | `src/crews/intake_crew.py:98`<br>`invoice_text = guardrails.safe_for_cloud(` | The invoice text is cleaned, and the claim stops if anything personal survives. |
| PII: the error that stops a claim | `src/guardrails.py:133`<br>`class PrivacyError(RuntimeError):` | |
| PII: where it is raised | `src/guardrails.py:239`<br>`raise PrivacyError(` | "The claim has been stopped rather than sent to a model." |
| PII: last line of defence | `src/guardrails.py:402`<br>`def _before_llm_call(context) -> Any:` | Masks phone, email and ID patterns in every message just before it is sent. |
| Task guardrail: coverage report | `src/crews/assessment_crew.py:154`<br>`guardrail=guardrails.coverage_report_guardrail,` | Must have PERIOD, PERIL and EXCLUDED LINES (`src/guardrails.py:277`). |
| Task guardrail: fraud report | `src/crews/assessment_crew.py:180`<br>`guardrail=guardrails.fraud_report_guardrail,` | Must have PRICES, PRIOR CLAIMS, CONSISTENCY and RISK (`src/guardrails.py:281`). |
| Task guardrail: the decision | `src/crews/assessment_crew.py:226`<br>`guardrail=guardrails.decision_guardrail,     # every line, exact amounts, no PII` | |
| Output check: exact amounts | `src/guardrails.py:310-311`<br>`given = sorted(round(float(i["amount"]), 2) for i in invoice)` | Every invoice line must be in the decision with its exact amount. |
| Output check: fraud score range | `src/guardrails.py:319`<br>`score = d.get("fraud_score")` | Must be between 0 and 1. |
| Output check: coverage consistent | `src/guardrails.py:336`<br>`if status == "excluded" and any(x.get("admitted") for x in lines):` | An excluded claim may not admit any line; an uncertain one must say why (line 338). |
| Output check: no PII in the decision | `src/guardrails.py:344`<br>`pii = find_pii(text_out)` | |
| Output check: sent back | `src/guardrails.py:364`<br>`return False, "Fix these problems in your decision: " + " \| ".join(problems)` | The agent gets the reasons and retries. |
| Tool-result check | `src/guardrails.py:371`<br>`def _after_tool_call(context) -> Any:` | Every tool result must be a JSON object; an error is returned to the agent with "correct the arguments". |
| Tool-result check (ADK) | `agents/claimant_assistant/agent.py:93`<br>`def check_tool_result(tool, args, tool_context, tool_response):` | |
| Photo privacy clearance | `src/vision.py:107`<br>`def photo_clearance(name: str) -> dict:` | A photo may go to a cloud model only if it is from the public research dataset. |
| ...the rule | `src/vision.py:132`<br>`"cleared": from_dataset or consented,` | A claimant's photograph is not cleared: its number plate has not been redacted. |
| ...the refusal | `src/vision.py:247-248`<br>`if not (local_provider() or clearance["cleared"]):` | Raises `PhotoUnsafeForCloud`, and the photo is never sent. |

SAY: "Claimant text is screened on line 444 before any agent sees it, a claim with surviving personal data is stopped by `PrivacyError` on line 239, and the Lead's decision has to pass the decision guardrail on line 226 before the Flow acts on it."

---

## 9. Observability

| Item | Where | What it does |
|---|---|---|
| Tracing switched on | `src/observability.py:296`<br>`def start(service: str = "claimsight", exporter=None) -> bool:` | Sets up OpenTelemetry once per process. |
| Called before the frameworks load | `run_flow.py:29`<br>`observability.start()                     # before the crews or ADK are imported` | Also at `run_assistant.py:32` and `src/a2a_server.py:191`. |
| The OTel provider | `src/observability.py:327`<br>`_provider = TracerProvider(resource=Resource.create({` | |
| CrewAI instrumented | `src/observability.py:334`<br>`CrewAIInstrumentor().instrument(tracer_provider=_provider)` | Spans for the Flow, crews, agents and tasks. |
| LLM client instrumented | `src/observability.py:337`<br>`OpenAIInstrumentor().instrument(tracer_provider=_provider)` | Spans for each model call. |
| Langfuse endpoint | `src/observability.py:260`<br>`return f"{(for_host or host()).rstrip('/')}/api/public/otel/v1/traces"` | Langfuse's OpenTelemetry endpoint. |
| Langfuse host setting | `src/observability.py:248`<br>`for name in ("LANGFUSE_HOST", "LANGFUSE_BASE_URL"):` | `LANGFUSE_HOST`, falling back to `LANGFUSE_BASE_URL`. |
| Langfuse auth | `src/observability.py:267`<br>`return {"Authorization": f"Basic {token}"}` | Basic auth from the public and secret keys. |
| Exporter wired in | `src/observability.py:218`<br>`OTLPSpanExporter(endpoint=endpoint(), headers=auth_header()), content))` | |
| The scrubbing exporter | `src/observability.py:145`<br>`def _scrubbing_exporter(inner, send_content: bool):` | Wraps the exporter so every span is cleaned on the way out... |
| ...what it scrubs with | `src/observability.py:90`<br>`cleaned, found = guardrails.redact_pii(value)` | ...using the same PII redaction as the guardrails. |
| One span per claim | `src/claim_flow.py:413`<br>`with observability.claim_span(claim_id):` | Groups a claim's spans under one parent (and `:374` for a resumed claim). |
| The offline switch | `src/observability.py:232`<br>`return os.getenv("CLAIMSIGHT_OFFLINE", "").lower() in {"1", "true", "yes"}` | `CLAIMSIGHT_OFFLINE=1` means nothing is exported; every offline test sets it. |
| ...where it is obeyed | `src/observability.py:312`<br>`if exporter is None and offline():` | No exporter is installed when offline. |

SAY: "`observability.start()` on line 296 instruments CrewAI and the model client with OpenTelemetry, every span passes through the scrubbing exporter on line 145 before it reaches Langfuse, and `CLAIMSIGHT_OFFLINE` on line 232 turns export off."

---

## 10. Evaluation

| Item | Where | What it does |
|---|---|---|
| The three configurations | `evaluation/evaluate_phase2.py:78`<br>`CONFIGS = ("A", "B", "C")` | |
| Which can run | `evaluation/evaluate_phase2.py:79`<br>`AVAILABLE = ("A", "B")` | C (ClaimSight with vision) is recorded as not available (line 80). |
| Config A: single LLM | `evaluation/evaluate_phase2.py:159`<br>`def build_single_llm_crew(verbose: bool = False, monitor=None):` | One agent (`SINGLE_LLM_ROLE`, line 96) given the same evidence and returning the same AssessmentDecision. |
| Config A: same evidence | `evaluation/evaluate_phase2.py:185`<br>`def tool_evidence(inputs: dict) -> dict:` | A cannot call tools, so it is given the results of the three tools B's analysts call. |
| Config B: ClaimSight | `evaluation/evaluate_phase2.py:287`<br>`return REAL_INTAKE_STEP, {"A": single_llm_step, "B": REAL_ASSESSMENT_STEP}[config]` | B is the real assessment step; A swaps in the single-LLM step. Everything after (calculator, router) is the same Flow. |
| Settings recorded | `evaluation/evaluate_phase2.py:333-334`<br>`return {**shared, "config": "A",` | Each config's description and fingerprint are saved with its results. |
| Restricted to development claims | `evaluation/evaluate_phase2.py:358`<br>`return [e for e in json.loads(MANIFEST.read_text()) if e["split"] == "dev"]` | Only the 20 development claims are ever evaluated here. |
| Anything else refused | `evaluation/evaluate_phase2.py:368`<br>`raise SystemExit(f"not development claims, so not evaluated here: {', '.join(refused)}")` | Asking for a held-out claim stops the run. |
| Labels read only to score | `evaluation/evaluate_phase2.py:373`<br>`"""The labels, read only to score. Nothing here reaches a crew."""` | `truth_for` reads `ground_truth` after the run. |
| What is measured | `evaluation/evaluate_phase2.py:817-838`<br>`TABLE = [` | Correct decisions, escalation rate, coverage accuracy, fraud precision and recall, line-item F1, payout error (mean and max), seconds per claim, and tokens (prompt, completion, reasoning, requests). |
| Scoring | `evaluation/evaluate_phase2.py:673`<br>`def score(records: list, truths: dict) -> dict:` | |

SAY: "Config A is one LLM built on line 159 and config B is the real crew on line 287, both run through the same Flow, and line 358 restricts the evaluation to the 20 development claims."

**Note:** line 80 still says "the Damage Analyst does not exist yet". The Damage Analyst now exists, but config C has not been added to the harness, so C is still not runnable.

---

## 11. Cloud deployment

| Item | Where | What it does |
|---|---|---|
| Cloud deployment | **NOT IMPLEMENTED** | There is no Dockerfile, no cloud build or app config, and no deployment script anywhere in the project. Everything runs locally. |
| How it runs today: the Flow | `run_flow.py:29`<br>`observability.start()                     # before the crews or ADK are imported` | `uv run python run_flow.py CLM-5000` runs the CrewAI Flow in one local process. |
| How it runs today: A2A server | `src/a2a_server.py:49`<br>`PORT = int(os.getenv("A2A_PORT", "9999"))` | `uv run python src/a2a_server.py` serves the CrewAI side on localhost:9999. |
| How it runs today: the assistant | `agents/claimant_assistant/agent.py:41`<br>`A2A_URL = os.getenv("CLAIMSIGHT_A2A_URL", "http://localhost:9999")` | `uv run python run_assistant.py` runs ADK locally and calls the A2A server at this URL. |
| Ready for a public URL | `src/a2a_server.py:50`<br>`PUBLIC_URL = os.getenv("A2A_PUBLIC_URL", f"http://{HOST}:{PORT}")` | The AgentCard's URL is configurable, which is what a deployed server would set. |
| The dashboard | `.streamlit/config.toml:4`<br>`address = "127.0.0.1"` | Bound to this machine only. |

What would have to change: package the A2A server in a container and run it on a managed service (for example Cloud Run); set `A2A_PUBLIC_URL` to its address and `CLAIMSIGHT_A2A_URL` on the ADK side to match; supply the Groq, Gemini and Langfuse keys from a secret store instead of `.env`; move `data/` (decisions, review queue, logs, evidence) from the local disk to a database or bucket; replace local Ollama embeddings with a hosted embedding model; and run the MCP server over HTTP (`--http`, `src/mcp_server.py:196`) instead of as a stdio subprocess.

SAY: "Cloud deployment is not implemented: it all runs locally, but the A2A server already takes its public URL from `A2A_PUBLIC_URL` on line 50, so deploying it is mostly packaging and configuration."

---

## A. The Damage Analyst and the photograph

| Item | Where | What it does |
|---|---|---|
| The fixed 12-part list | `src/vision.py:62`<br>`PARTS = ["front bumper", "rear bumper", "headlight", "taillight", "side mirror",` | The model answers about these parts only, never parts taken from the invoice. |
| The three-verdict prompt | `src/vision.py:71`<br>`PROMPT = (` | Asks for damaged, undamaged or not_visible on every part. |
| "Undamaged" must be certain | `src/vision.py:78`<br>`"  undamaged    the part is clearly visible in the frame AND clearly intact\n"` | |
| Front or rear unclear | `src/vision.py:86`<br>`"or taillight), put BOTH candidates in not_visible rather than guessing.\n"` | Stops a close-up from becoming a front/rear guess. |
| Image downscaled | `src/vision.py:53`<br>`MAX_EDGE = int(os.getenv("CLAIMSIGHT_PHOTO_MAX_EDGE", "768"))` | |
| The vision call | `src/vision.py:278`<br>`response = litellm.completion(messages=messages, **kwargs)` | The one model call that looks at the photo. |
| Answer forced into three lists | `src/vision.py:185`<br>`def partition(answer: dict, readable: bool = True) -> dict:` | Unknown names are dropped; a part not mentioned or contradicted goes to not_visible, so the three lists always cover all 12 parts exactly once. |
| Called during intake | `src/crews/intake_crew.py:113`<br>`for seen in vision.inspect_claim_photos(photos, monitor=monitor)]` | Data gathering in `fetch_inputs`, like reading the invoice PDF. |
| The agent | `src/crews/intake_crew.py:220`<br>`role="Damage Analyst",` | The sixth CrewAI agent, added only when a claim has photos (line 218). |
| It never sees the invoice | `src/crews/intake_crew.py:252`<br>`context=[],              # never the invoice or the intake record` | |
| Its record checked in code | `src/crews/intake_crew.py:269`<br>`def photo_damage(recorded, inspected: list, monitor=None) -> list:` | If the agent changes a verdict, the vision result is used instead. |
| Compared with the invoice IN CODE | `src/claim_flow.py:179`<br>`result = check_billed_parts_against_photo(` | `check_photos`, run after intake on every pass. |
| The comparison itself | `src/tools/photo_check.py:14`<br>`def check_billed_parts_against_photo(line_items: List[dict],` | |
| The flag rule | `src/tools/photo_check.py:75`<br>`unsupported = sorted(part for part in billed if part in seen_intact)` | Only a billed part seen intact is evidence against the line. |
| The unchecked rule | `src/tools/photo_check.py:77`<br>`unchecked = sorted(part for part in billed` | A billed part not seen is unchecked, not flagged. |
| Run in the Flow | `src/claim_flow.py:492`<br>`self.state.photo_check = check_photos(self.state.intake, supplied)` | Saved in the state and logged (line 496). |
| Raises an evidence request | `src/claim_flow.py:500`<br>`gaps = gaps + list(c.get("requests") or [])` | A part not visible becomes "photograph showing the ..." through the normal evidence loop. |
| Raises a fraud flag | `src/claim_flow.py:551`<br>`flag = f"{flag} (photo check, in code)"` | A part shown intact is added to the decision's fraud flags. |
| Routes to a person | `src/claim_flow.py:287-288`<br>`if photo_problems:` | `decide_route`: "photo check: the photograph shows the front door undamaged, but it is billed". |

SAY: "The model only says what it can see, on line 71 of `vision.py`; the comparison with the invoice is Python, on line 75 of `photo_check.py`, and only a part the photo shows intact can raise a flag."

---

## B. "LLMs judge, code calculates"

| Decision | Where | What it does |
|---|---|---|
| Completeness gate | `src/claim_flow.py:115`<br>`def completeness_gaps(intake: dict) -> list:` | Policy found, line items, total, incident date and registration, all checked in code. |
| ...overrides the agent | `src/claim_flow.py:485`<br>`self.log(f"intake: Intake Officer said complete={agent_said}; "` | The Intake Officer's own verdict is logged, then overridden. |
| Settlement calculator | `src/tools/settlement.py:222`<br>`def calculate_settlement(line_items, policy, gst_rate: float = GST) -> Settlement:` | Depreciation by material and age, GST, deductible, capped at the IDV. |
| ...the payout line | `src/tools/settlement.py:263`<br>`payout = after_dep + gst - deductible` | |
| ...called by the Flow | `src/claim_flow.py:559`<br>`self.state.settlement = settle(self.state.intake["policy_no"], d["line_decisions"])` | The Lead decides which lines; code computes the money. |
| Invoice tax check | `src/tools/settlement.py:106`<br>`def check_invoice_tax(line_items, total, gst_rate: float = GST) -> dict:` | Settles from the invoice's own arithmetic whether amounts are before or after tax. |
| Coverage doubts | `src/claim_flow.py:216`<br>`def coverage_doubts(intake: dict, decision: dict) -> list:` | A rejection needs a verified clause; otherwise a person decides. |
| The router | `src/claim_flow.py:268`<br>`def decide_route(decision: dict, settlement: dict, guardrail_flags: list,` | Pure function: guardrail flags, doubts, exclusion, photo problems, fraud threshold, high value. |
| The Lead's suggestion ignored | `src/claim_flow.py:581`<br>`why += f" (the Lead suggested {suggested}; the rule decides)"` | |
| Fraud threshold | `src/claim_flow.py:55`<br>`FRAUD_THRESHOLD = 0.5` | |
| High-value limit | `src/claim_flow.py:56`<br>`HIGH_VALUE = 50_000          # payouts above this always go to a person` | |
| Photo comparison | `src/tools/photo_check.py:75`<br>`unsupported = sorted(part for part in billed if part in seen_intact)` | |
| Coverage flag derived | `src/models.py:155`<br>`object.__setattr__(self, "is_covered", self.coverage_status == "covered")` | |
| Evidence accepted by rule | `src/evidence.py:282`<br>`ok, kept, why = checker(value)` | No model decides whether evidence is valid. |

SAY: "The agents decide what is covered and what looks suspicious, but the money is computed on line 263 of `settlement.py` and the route is decided by the plain function on line 268 of `claim_flow.py`, which overrules the Lead's suggestion on line 581."

---

## C. Policy citation verification

| Item | Where | What it does |
|---|---|---|
| The checker | `src/citations.py:98`<br>`def check(reason: str, verdict: str = "excluded", text: Optional[str] = None) -> dict:` | Looks the cited clause up in `knowledge/policy_wording.txt`. |
| Three separate questions | `src/citations.py:9-11`<br>`cited               does the reason name a clause at all?` | Cited, source verified, and supports the conclusion. |
| Found in the wording | `src/citations.py:184`<br>`source_verified = bool(found) and not missing` | |
| A covering clause cannot reject | `src/citations.py:203`<br>`why = (f"{passage['identifier']} is part of what the policy COVERS, "` | A real clause from the cover section cannot justify a rejection. |
| Citation not found | `src/citations.py:181`<br>`missing.append(f"IMT {n} is not in this policy")` | An invented clause is recorded as missing. |
| What happens then | `src/claim_flow.py:247-251`<br>`verdict = citations.check(reason, verdict="excluded")` | No verified, supporting clause gives a coverage doubt: "no reason supports refusing this claim". |
| ...and the claim goes to a person | `src/claim_flow.py:280-281`<br>`if doubts:` | "coverage not established", so human_review, never a rejection. |
| Shown in the report | `src/reporting.py:212`<br>`grounded=verdict["source_verified"] and verdict["supports_conclusion"]))` | Each coverage reason is marked verified or not in the report and on the dashboard. |

SAY: "A rejection is only accepted if the cited clause exists in the policy and takes cover away, which `citations.check` on line 98 tests; if not, line 251 records a doubt and the claim goes to a person."

---

## D. The dataset split

| Item | Where | What it does |
|---|---|---|
| Split size | `generate_data.py:34`<br>`N_DEV = 20` | Of `N_CLAIMS = 60` (line 33). |
| Split assigned | `generate_data.py:340`<br>`"split": "dev" if i < N_DEV else "test",` | The first 20 are development, the other 40 held out. |
| Manifest written | `generate_data.py:351`<br>`(DATA / "manifest.json").write_text(json.dumps(manifest, indent=2))` | `data/manifest.json` is the index of all 60 with their split. |
| Enforced: evaluation | `evaluation/evaluate_phase2.py:358`<br>`return [e for e in json.loads(MANIFEST.read_text()) if e["split"] == "dev"]` | The evaluation only ever selects development claims... |
| Enforced: evaluation refuses others | `evaluation/evaluate_phase2.py:368`<br>`raise SystemExit(f"not development claims, so not evaluated here: {', '.join(refused)}")` | ...and stops if asked for anything else. |
| Enforced: live dashboard runs | `dashboard/flow_runner.py:67`<br>`return any(e.get("claim_id") == claim_id and e.get("split") == "dev" for e in entries)` | Only development claims can be run live from the dashboard. |
| Enforced: dashboard picker | `dashboard/data_adapter.py:91`<br>`ids = [e["claim_id"] for e in manifest() if e["split"] == "dev"]` | Held-out claims cannot be opened in the dashboard. |

SAY: "The split is fixed when the data is generated, on line 340, and every place that runs or shows claims, including the evaluation on line 358, filters to `split == "dev"`."

Limit: the code can show that the held-out claims are filtered out wherever claims are selected. It cannot prove from code alone that nobody looked at them by hand while tuning prompts; that rests on this filtering plus the project's working rule.

---

## E. How the agents are stopped from seeing the answer

| Item | Where | What it does |
|---|---|---|
| The whitelist | `src/tools/claim_tools.py:80-86`<br>`submitted = {` | Only `claim_id`, `policy_no`, `incident_date`, `narrative` and `photos` are passed on. |
| Used by the intake crew | `src/crews/intake_crew.py:83`<br>`submission_text = ReadClaimSubmission()._run(claim_id)` | The Intake crew reads the claim only through this whitelist. |
| Fields it drops | `src/tools/claim_tools.py:79`<br>`claim = json.loads(path.read_text())` | Not passed on: `ground_truth` (fault type, is_fraudulent, phantom part, expected payout), `line_items` (which carry the per-line `admitted` answers and reasons), `split`, `invoice_subtotal`, `invoice_total`, `invoice_date` and `invoice_pdf`. |
| The invoice is read from the PDF | `src/crews/intake_crew.py:96`<br>`invoice_text = ReadInvoicePDF()._run(claim_id)` | Line items reach the agents from the invoice document, which carries no labels. |
| Assessment sees no answers | `src/claim_flow.py:299`<br>`lines = [{"description": i["description"], "amount": i["amount"], "material": i.get("material")}` | Only description, amount and material are passed on: no `admitted`, no `reason`. |
| Prior-claims tool | `src/tools/assessment_tools.py:185`<br>`other_parts = {_part_name(i["description"]) for i in other["line_items"]} - {None}` | Reads only the descriptions of earlier claims, never their labels. |
| Labels only for scoring | `evaluation/evaluate_phase2.py:373`<br>`"""The labels, read only to score. Nothing here reaches a crew."""` | The evaluation reads `ground_truth` only to score a finished run. |
| Labels in the terminal runners | `run_flow.py:41`<br>`truth = claim["ground_truth"]` | The terminal scripts (`run_flow.py`, `run_intake.py:53`, `run_assessment.py:102`, `run_damage_analyst.py:58`) also read the labels, to print the expected result beside the actual one. None of them puts labels into a crew's inputs: those are built only by `fetch_inputs` (through the whitelist above) and `assessment_inputs` (line 299 above). |

SAY: "The agents read a claim only through this whitelist on lines 80 to 86, which passes five fields and drops the ground truth and the per-line answers."

---

## If he asks for one line

| # | Requirement | Line |
|---|---|---|
| 1 | State | `src/claim_flow.py:420` `@persist()` |
| 2 | MCP | `src/mcp_server.py:52` `mcp = FastMCP("claimsight-tools", port=8765, log_level="WARNING")` |
| 3 | Pydantic structured output | `src/crews/assessment_crew.py:225` `output_pydantic=AssessmentDecision,` |
| 4 | Agentic design patterns | `src/claim_flow.py:573` `@router(assess, emit=["auto_approve", "human_review", "reject"])` |
| 5 | Callbacks and logging | `src/monitoring.py:216` `def crew_callbacks(monitor: Optional[ClaimMonitor]) -> dict:` |
| 6 | A2A | `src/a2a_server.py:123` `state = await asyncio.to_thread(run_claim_flow, claim_id)   # the Flow runs its own loop` |
| 7 | CrewAI and ADK only | `pyproject.toml:18` `"google-adk[a2a]>=2.13.0",` |
| 8 | Guardrails | `src/crews/assessment_crew.py:226` `guardrail=guardrails.decision_guardrail,     # every line, exact amounts, no PII` |
| 9 | Observability | `src/observability.py:334` `CrewAIInstrumentor().instrument(tracer_provider=_provider)` |
| 10 | Evaluation | `evaluation/evaluate_phase2.py:358` `return [e for e in json.loads(MANIFEST.read_text()) if e["split"] == "dev"]` |
| 11 | Cloud deployment | NOT IMPLEMENTED. Closest: `src/a2a_server.py:50` `PUBLIC_URL = os.getenv("A2A_PUBLIC_URL", f"http://{HOST}:{PORT}")` |
