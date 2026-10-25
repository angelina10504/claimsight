"""
ClaimSight Claimant Assistant (Google ADK).

The claimant-facing side of ClaimSight. It talks to the claimant, and hands the
actual assessment to the CrewAI system over A2A. Two frameworks, one protocol.

    claimant  <->  Claimant Assistant (ADK, Gemini)
                      |-- claimsight_adjudicator   A2A -> CrewAI ClaimFlow (src/a2a_server.py)
                      |-- get_claim_status         MCP -> the same MCP server CrewAI uses
                      |-- get_claim_report         MCP -> the evidence report for a decision

ADK callbacks, sharing the guardrails module with the CrewAI side:
  before_model_callback  screens each claimant message: prompt injection never
                         reaches the model; abusive words are masked first
  after_tool_callback    checks every tool result is a dict without an error,
                         and tells the model plainly when a tool failed
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]       # agents/claimant_assistant/agent.py -> project root
sys.path.insert(0, str(ROOT / "src"))
load_dotenv(ROOT / ".env")

from google.adk.agents import LlmAgent                                    # noqa: E402
from google.adk.agents.remote_a2a_agent import RemoteA2aAgent             # noqa: E402
from google.adk.models import LlmResponse                                 # noqa: E402
from google.adk.tools.agent_tool import AgentTool                         # noqa: E402
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams  # noqa: E402
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset              # noqa: E402
from google.genai import types                                            # noqa: E402
from mcp import StdioServerParameters                                     # noqa: E402

import guardrails                                                         # noqa: E402

MODEL = os.getenv("ADK_MODEL", "gemini-3.8-flash")
A2A_URL = os.getenv("CLAIMSIGHT_A2A_URL", "http://localhost:9999")

# Gemini sometimes answers 503 "high demand" or 429. Retry those a few times,
# waiting longer each time (2s, 4s, 8s, 16s), before giving up.
RETRY = types.GenerateContentConfig(
    http_options=types.HttpOptions(
        retry_options=types.HttpRetryOptions(attempts=5, initial_delay=2.0, max_delay=30.0,
                                             http_status_codes=[429, 500, 503, 504])))


# ------------------------------------------------------------ callbacks
def screen_claimant_message(callback_context, llm_request):
    """before_model_callback: screen the claimant's latest message.

    Prompt injection: the message never reaches the model; a fixed reply is sent.
    Abuse: the abusive words are masked in place and the request is answered."""
    last_user = next((c for c in reversed(llm_request.contents or []) if c.role == "user"), None)
    parts = [p for p in (last_user.parts if last_user else []) if getattr(p, "text", None)]
    if not parts:
        return None                                   # e.g. a tool result turn: nothing to screen
    result = guardrails.screen_text(" ".join(p.text for p in parts))
    if result["verdict"] == "clean":
        return None

    flags = callback_context.state.get("guardrail_flags", [])
    callback_context.state["guardrail_flags"] = flags + [result["verdict"]]

    if result["verdict"] == "injection":
        reply = ("I can only help with your claim itself. Please tell me your claim id "
                 "(for example CLM-5004) and what you need, and I will check it for you.")
        return LlmResponse(content=types.Content(role="model", parts=[types.Part(text=reply)]))

    for part in parts:                                # abusive: mask, then let the model answer
        part.text, _ = guardrails.safe_narrative(part.text)
    return None


def _tool_payload(tool_response):
    """The JSON a tool returned. MCP tools wrap it as {"content": [{"text": ...}], "isError": ...}."""
    if isinstance(tool_response, dict) and isinstance(tool_response.get("content"), list):
        import json
        for item in tool_response["content"]:
            text = item.get("text") if isinstance(item, dict) else None
            if text:
                try:
                    return json.loads(text), bool(tool_response.get("isError"))
                except json.JSONDecodeError:
                    return text, bool(tool_response.get("isError"))
        return None, bool(tool_response.get("isError"))
    return tool_response, False


def check_tool_result(tool, args, tool_context, tool_response):
    """after_tool_callback: type-check what a tool returned."""
    payload, is_error = _tool_payload(tool_response)
    if is_error or (isinstance(payload, dict) and "error" in payload):
        problem = payload.get("error") if isinstance(payload, dict) else "tool error"
        return {"error": problem, "note": "The tool rejected the input. Ask the claimant to "
                                          "confirm the claim id, then try again."}
    if payload is None or not isinstance(payload, (dict, str)):
        return {"error": f"{tool.name} returned unexpected data; do not rely on it."}
    return None


# ------------------------------------------------------------ tools
adjudicator = RemoteA2aAgent(
    name="claimsight_adjudicator",
    description=("ClaimSight's CrewAI assessment system. Send it a request naming one claim id, "
                 "e.g. 'Please assess claim CLM-5004'. It returns the decision."),
    agent_card=f"{A2A_URL}/.well-known/agent-card.json",
)

claim_status = McpToolset(
    connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(command="python", args=[str(ROOT / "src" / "mcp_server.py")]),
        timeout=30,
    ),
    tool_filter=["get_claim_status", "get_claim_report"],
)

root_agent = LlmAgent(
    name="claimant_assistant",
    model=MODEL,
    description="Helps a policyholder submit a motor insurance claim and follow its progress.",
    instruction="""You are ClaimSight's claimant assistant for motor insurance claims.

You help the policyholder in two ways:
1. Assess a claim: when they ask you to process or assess a claim and give a claim id
   (format CLM-1234), call the claimsight_adjudicator tool with the request
   "Please assess claim <id>". It runs the full assessment and returns the outcome.
2. Check status: when they ask how an existing claim is doing, call get_claim_status
   with the claim id.
3. Explain a decision: when they ask why, or disagree with an outcome, call
   get_claim_report with the claim id. It returns the coverage finding and the
   policy clause behind it, each invoice line with its reason, the settlement
   breakdown and anything still needed. Quote those reasons; never invent one.

If they have not given a claim id, ask for it. Never guess one.

If claimsight_adjudicator says the assessment could not be completed or failed,
tell the claimant exactly that and suggest trying again later. Do NOT call
get_claim_status to fill the gap: a status on file may be from an older
assessment, and must never be presented as the result of this request.

When you report back:
- Say clearly whether the claim was approved, rejected, sent for review, or needs documents.
- Give the payout amount if there is one, exactly as returned.
- If documents or details are missing, list exactly what is needed, one per line.
  Say that once they have been sent, asking you to assess the claim again will
  carry it on from where it stopped: nothing has to be started over.
- If the claim went for review, explain that an adjuster will check it.
- Give the reasons the assessment actually recorded, in plain language: which lines
  were not paid and why, and what the policy said. If a line was denied, say so even
  when it is unwelcome news.
- If coverage could not be settled from the policy wording, say that a claims officer
  will decide it, and do not guess which way.
- Be brief and kind. Do not invent reasons, amounts or timelines the tools did not give you.
""",
    tools=[AgentTool(agent=adjudicator), claim_status],
    generate_content_config=RETRY,
    before_model_callback=screen_claimant_message,
    after_tool_callback=check_tool_result,
)
