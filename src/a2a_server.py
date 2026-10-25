"""
ClaimSight as an A2A agent.

Wraps the whole CrewAI ClaimFlow behind the Agent2Agent (A2A) protocol, so any
A2A client can hand it a claim and get back a decision. The ADK Claimant
Assistant (agents/claimant_assistant) is that client: an ADK agent talking to a
CrewAI system across a standard protocol.

What a request looks like
  Client sends a message mentioning a claim id, e.g. "Please assess CLM-5004".
  Server replies with an A2A Task that moves submitted -> working -> completed,
  carrying two artifacts: a plain-text summary and the decision as JSON.

The A2A boundary is untrusted input like any other, so the incoming text is
screened by the same guardrails before anything runs. The human review gate
runs non-interactively here: such claims come back as "awaiting_review".

Run:  uv run python src/a2a_server.py          serves on http://localhost:9999
      agent card: http://localhost:9999/.well-known/agent-card.json

Built with a2a-sdk, the protocol library ADK also uses. It is a protocol
library, not an agent framework: the agents are CrewAI (here) and ADK (client).
"""

import asyncio
import os
import re
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent
sys.path.insert(0, str(SRC))

from a2a.helpers.proto_helpers import new_data_part, new_text_message, new_text_part   # noqa: E402
from a2a.server.agent_execution import AgentExecutor, RequestContext                 # noqa: E402
from a2a.server.events import EventQueue                                              # noqa: E402
from a2a.server.request_handlers import DefaultRequestHandler                         # noqa: E402
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes         # noqa: E402
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater                           # noqa: E402
from a2a.types import (                                                               # noqa: E402
    AgentCapabilities, AgentCard, AgentInterface, AgentSkill, Role, Task, TaskState, TaskStatus,
)
from starlette.applications import Starlette                                          # noqa: E402

import guardrails                                                                     # noqa: E402

HOST = os.getenv("A2A_HOST", "localhost")
PORT = int(os.getenv("A2A_PORT", "9999"))
PUBLIC_URL = os.getenv("A2A_PUBLIC_URL", f"http://{HOST}:{PORT}")
CLAIM_RE = re.compile(r"\bCLM-\d{4}\b", re.IGNORECASE)
CLAIMS = SRC.parent / "data" / "claims"


FINAL_STATUSES = {"approved", "rejected", "awaiting_evidence", "awaiting_review",
                  "approved_by_adjuster", "rejected_by_adjuster"}


def run_claim_flow(claim_id: str) -> dict:
    """Run the CrewAI ClaimFlow for one claim and return its final state."""
    from claim_flow import ClaimFlow
    flow = ClaimFlow()
    flow.kickoff(inputs={"claim_id": claim_id, "interactive": False, "use_saved_intake": True})
    return flow.state.model_dump()


def summarise(state: dict) -> str:
    """One short paragraph a claimant-facing agent can relay."""
    claim, status = state["claim_id"], state.get("status")
    payout = state.get("final_payout", 0.0)
    decision = state.get("decision") or {}
    if status == "approved":
        return f"Claim {claim} is approved. Payout: Rs {payout:,.2f}."
    if status == "rejected":
        reasons = "; ".join(decision.get("coverage_reasons", [])[:2])
        return f"Claim {claim} is not covered. {reasons}".strip()
    if status == "awaiting_evidence":
        missing = ", ".join((state.get("intake") or {}).get("missing_fields", []))
        return f"Claim {claim} needs more documents before it can be assessed: {missing}."
    if status == "awaiting_review":
        proposed = (state.get("settlement") or {}).get("payout", 0.0)
        return (f"Claim {claim} has been sent to a claims adjuster for review. "
                f"Proposed payout: Rs {proposed:,.2f}. The claimant will hear back after review.")
    return f"Claim {claim}: status {status}."


class ClaimSightExecutor(AgentExecutor):
    """Bridges A2A requests to the CrewAI ClaimFlow."""

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        # a2a-sdk 1.x: the first event for a new task must be the Task itself
        if not context.current_task:
            await event_queue.enqueue_event(Task(
                id=context.task_id, context_id=context.context_id,
                status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
                history=[context.message] if context.message else []))
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)

        def reply(text: str):
            return new_text_message(text, context_id=context.context_id,
                                    task_id=context.task_id, role=Role.ROLE_AGENT)

        text = context.get_user_input()
        screening = guardrails.screen_text(text)
        if screening["verdict"] == "injection":
            await updater.reject(reply("Request refused: it contains instructions aimed at the "
                                       "assessment system. Send the claim id only."))
            return

        match = CLAIM_RE.search(text)
        if not match:
            await updater.requires_input(reply("Which claim should I assess? Send its id, e.g. CLM-5004."))
            return
        claim_id = match.group(0).upper()
        if not (CLAIMS / f"{claim_id}.json").exists():
            await updater.reject(reply(f"No claim {claim_id} is on file."))
            return

        await updater.start_work(reply(f"Assessing {claim_id}: intake, coverage and fraud checks..."))
        try:
            state = await asyncio.to_thread(run_claim_flow, claim_id)   # the Flow runs its own loop
        except Exception as exc:                                       # noqa: BLE001
            reason = str(exc).splitlines()[0][:160] if str(exc) else type(exc).__name__
            await updater.failed(reply(f"Assessment of {claim_id} could not be completed right now "
                                       f"({reason}). Nothing has been decided; please try again later."))
            return

        if state.get("status") not in FINAL_STATUSES:     # the Flow stopped before a decision
            await updater.failed(reply(f"Assessment of {claim_id} could not be completed right now "
                                       f"(the assessment system stopped before a decision). "
                                       f"Nothing has been decided; please try again later."))
            return

        decision = state.get("decision") or {}
        result = {
            "claim_id": claim_id,
            "status": state.get("status"),
            "route": state.get("route"),
            "payout": state.get("final_payout", 0.0),
            "proposed_payout": (state.get("settlement") or {}).get("payout", 0.0),
            "is_covered": decision.get("is_covered"),
            "fraud_score": decision.get("fraud_score"),
            "denied_lines": [{"line": x["description"], "reason": x.get("reason", "")}
                             for x in decision.get("line_decisions", []) if not x.get("admitted")],
            "missing_evidence": (state.get("intake") or {}).get("missing_fields", []),
            "guardrail_flags": state.get("guardrail_flags", []),
        }
        summary = summarise(state)
        await updater.add_artifact([new_text_part(summary)], name="summary")
        await updater.add_artifact([new_data_part(result)], name="decision")
        await updater.complete(reply(summary))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.cancel()


def agent_card() -> AgentCard:
    return AgentCard(
        name="claimsight_adjudicator",
        description=("ClaimSight motor-insurance claim adjudicator (CrewAI). Give it a claim id; it "
                     "runs intake, policy coverage (RAG), fraud checks and settlement, and returns "
                     "the decision."),
        version="1.0.0",
        supported_interfaces=[AgentInterface(url=PUBLIC_URL, protocol_binding="JSONRPC",
                                             protocol_version="1.0")],
        capabilities=AgentCapabilities(streaming=False),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain", "application/json"],
        skills=[AgentSkill(
            id="adjudicate_claim", name="Adjudicate a motor insurance claim",
            description=("Assess a claim already on file: coverage, fraud signals, line-by-line "
                         "settlement. Returns approved, rejected, awaiting_review or awaiting_evidence."),
            tags=["insurance", "claims", "crewai"],
            examples=["Please assess claim CLM-5004", "Process CLM-5000"])],
    )


def build_app() -> Starlette:
    card = agent_card()
    handler = DefaultRequestHandler(agent_executor=ClaimSightExecutor(),
                                    task_store=InMemoryTaskStore(), agent_card=card)
    app = Starlette()
    app.routes.extend([*create_agent_card_routes(card), *create_jsonrpc_routes(handler, "/", enable_v0_3_compat=True)])
    return app


app = build_app()

if __name__ == "__main__":
    import uvicorn
    print(f"ClaimSight A2A server on {PUBLIC_URL}  (card: {PUBLIC_URL}/.well-known/agent-card.json)")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
