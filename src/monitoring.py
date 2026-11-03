"""
Task monitoring and logging for ClaimSight.

Every claim gets a JSON-lines log at data/logs/<claim_id>.jsonl. Each line is
one event with a timestamp, so the full run can be replayed, debugged, timed
and costed afterwards (show_log.py prints it as a timeline).

Three kinds of hook feed the log:

  step_callback  CrewAI calls it after every agent step: each tool call with
                 its input and result, and each agent's final answer. With a
                 model that uses native tool calling (Groq's gpt-oss does),
                 CrewAI never sends a tool step here, so tool calls are
                 recorded from the after_tool_call hook instead; both routes
                 go through record_tool_call, which drops duplicates.
  task_callback  CrewAI calls it when a task finishes: which agent, how long
                 it took, and its output.
  event bus      Listeners for errors CrewAI handles internally and retries
                 (LLM call failures such as rate limits or malformed tool
                 calls, and tool errors). These never reach the two callbacks
                 above, so without the listeners they would leave no trace.

The Flow adds its own entries: each step, each router decision and its reason,
crew timings and token use, and the final outcome.
"""

import contextvars
import json
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Set

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / "data" / "logs"
MAX_TEXT = 600          # characters kept from long outputs

# Every count the provider reports for a crew. A reasoning model spends tokens
# that never appear in the completion, and cached prompt tokens are charged
# differently, so the total alone is not enough. No cost is ever estimated.
USAGE_FIELDS = ("total_tokens", "prompt_tokens", "completion_tokens",
                "cached_prompt_tokens", "reasoning_tokens", "successful_requests")

_lock = threading.Lock()
_listeners_registered = False

# The claim running in THIS request's context. A module global was fine while
# only one claim ran at a time, but a deployed A2A server assesses several at
# once: the A2A executor runs each claim on its own thread (asyncio.to_thread),
# and a global would let two claims' events interleave into the wrong log. The
# monitor is bound per request instead, and current() resolves it in three steps.
#
# 1. A context variable, the per-request context asked for. It is the source of
#    truth within a single task: a synchronous crew sets it and reads it straight
#    back, and two claims each read their own.
_active_var: contextvars.ContextVar[Optional["ClaimMonitor"]] = contextvars.ContextVar(
    "claimsight_active_monitor", default=None)

# 2. The thread the request runs on. A context variable does not cross task
#    boundaries: a CrewAI Flow runs its @start and @listen steps as separate
#    asyncio tasks, each a fresh copy of the context, so a monitor created in the
#    intake step is invisible to the context var of a later step. But every task
#    of one claim's flow runs on ONE thread (asyncio is single-threaded per loop)
#    and concurrent claims each have their own thread, so the thread is a stable,
#    leak-proof key for the whole run. This is what keeps two concurrent claims'
#    flow-level logs separate.
_by_thread: Dict[int, "ClaimMonitor"] = {}

# 3. The sole running claim. CrewAI runs the two parallel analysts
#    (async_execution=True) on threads it spawns, which inherit neither the
#    context var nor the creating thread's id, so their tool calls reach the
#    after_tool_call hook with nothing to go on. _running holds every monitor
#    that has started and not finished; the fallback is used ONLY when exactly
#    one claim is running, so it can never attribute one claim's event to
#    another. The instant a second claim starts it switches off and each claim is
#    resolved strictly by steps 1-2; the cost is that an analyst-thread tool call
#    made while two claims overlap goes unlogged rather than mislogged. One claim
#    at a time - every local run, the demo, a lightly loaded free tier - keeps
#    today's full trace.
_running: Set["ClaimMonitor"] = set()


def _trace_id() -> Optional[str]:
    """The Langfuse trace this run belongs to, so the local log and the trace
    can be read side by side. None when tracing is off."""
    try:
        import observability
        return observability.trace_id()
    except Exception:            # tracing is optional; never break a run for it
        return None


def looks_like_tool_error(result: Any) -> bool:
    """Did this tool call fail?

    Two shapes count: a string starting with ERROR (how CrewAI reports a tool
    that raised) and a JSON object with an "error" key (how the ClaimSight
    tools report bad input). Checking only the first missed every rejected
    input, so failed calls were logged as successes."""
    if isinstance(result, str):
        if result.strip().upper().startswith("ERROR"):
            return True
        try:
            result = json.loads(result)
        except (json.JSONDecodeError, TypeError, ValueError):
            return False
    return isinstance(result, dict) and "error" in result


def _name_key(tool: Any) -> str:
    """A tool name with punctuation and casing removed, so the same tool reported
    as 'python_src/mcp_server.py_check_prices' and
    'python_src_mcp_server_py_check_prices' is recognised as one tool."""
    return re.sub(r"[^a-z0-9]", "", str(tool).lower())[-40:]


def _short(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT] + " ...[truncated]"


class ClaimMonitor:
    def __init__(self, claim_id: str, fresh: bool = True, **started_fields):
        self.claim_id = claim_id
        self.path = LOGS / f"{claim_id}.jsonl"
        LOGS.mkdir(parents=True, exist_ok=True)
        if fresh and self.path.exists():
            self.path.unlink()
        self.started = time.time()
        self.last_task_end = self.started
        self.counts = {"tool_calls": 0, "tasks": 0, "errors": 0, "retried_llm_failures": 0}
        self._seen_tool_calls: list = []        # recent (tool, input, result) keys
        self.llm_error_kinds: set = set()       # e.g. {"per_minute_rate_limit"}
        self.crew_usage: dict = {}              # crew name -> usage_fields(...)
        # Make this the active claim for the current context AND for the thread
        # the run is on, and register it as running for the single-claim fallback
        # (see the three resolution steps above current()). The context var is
        # not reset on finish: the old global was never cleared either, so any
        # lookup after a run still resolves.
        _active_var.set(self)
        with _lock:
            _by_thread[threading.get_ident()] = self
            _running.add(self)
        register_listeners()
        self.record("run_started", trace_id=_trace_id(), **started_fields)

    # ------------------------------------------------------------ writing
    def record(self, event: str, **fields) -> None:
        entry = {
            "time": datetime.now().isoformat(timespec="milliseconds"),
            "elapsed_s": round(time.time() - self.started, 2),
            "claim_id": self.claim_id,
            "event": event,
            **fields,
        }
        with _lock:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, default=str) + "\n")

    def record_tool_call(self, tool: str, tool_input: Any, result: Any,
                         error: Optional[bool] = None, agent: Any = None) -> None:
        """The one place a tool call is written to the log.

        Two things can report the same call: CrewAI's step callback (when the
        agent works in the ReAct style) and the after_tool_call guardrail hook
        (always). Recording it twice would make the evidence trail and the tool
        count wrong, so a call already seen is dropped.

        The two routes do not agree on the wording: the step callback gives the
        tool's name with its launch path punctuation intact and the result after
        the guardrail annotated it, while the hook gives the sanitised name and
        the raw result. So the key ignores punctuation in the name, and ignores
        the result entirely. The hook fires first, which is what we want: the
        raw tool output is the evidence, and the guardrail's note on it is
        logged separately as its own event."""
        if error is None:
            error = looks_like_tool_error(result)
        key = (_name_key(tool), _short(tool_input))
        if key in self._seen_tool_calls:
            return
        self._seen_tool_calls = (self._seen_tool_calls + [key])[-8:]
        self.counts["tool_calls"] += 1
        if error:
            self.counts["errors"] += 1
        self.record("tool_call", tool=tool, input=_short(tool_input),
                    result=_short(result), tool_error=error,
                    agent=str(getattr(agent, "role", "") or "") or None)

    # ------------------------------------------------------------ CrewAI callbacks
    def step_callback(self, step: Any) -> None:
        """After each agent step. CrewAI passes a tool call (AgentAction), a raw
        tool result (ToolResult) or a final answer (AgentFinish)."""
        kind = type(step).__name__
        if hasattr(step, "tool"):                       # AgentAction: a tool was called
            self.record_tool_call(step.tool, getattr(step, "tool_input", ""),
                                  getattr(step, "result", None) or "")
        elif hasattr(step, "output"):                   # AgentFinish: the agent's answer
            self.record("agent_answer", output=_short(step.output))
        elif hasattr(step, "result"):                   # ToolResult before it is attached
            return                                      # logged with its AgentAction above
        else:
            self.record("agent_step", kind=kind, detail=_short(str(step)))

    def task_callback(self, output: Any) -> None:
        """When a task finishes."""
        now = time.time()
        self.counts["tasks"] += 1
        pydantic_model = getattr(output, "pydantic", None)
        self.record(
            "task_done",
            agent=getattr(output, "agent", ""),
            task=_short((getattr(output, "description", "") or "").split("\n")[0]),
            seconds_since_previous_task=round(now - self.last_task_end, 2),
            structured=type(pydantic_model).__name__ if pydantic_model else None,
            output=_short(getattr(output, "raw", "")),
        )
        self.last_task_end = now

    # ------------------------------------------------------------ errors and summary
    def error(self, where: str, exc: Exception) -> None:
        self.counts["errors"] += 1
        self.record("error", where=where, error_type=type(exc).__name__, error=_short(str(exc)))

    def finish(self, **fields) -> dict:
        summary = {"total_seconds": round(time.time() - self.started, 2),
                   "trace_id": _trace_id(), "llm_errors": sorted(self.llm_error_kinds),
                   **self.counts, **fields}
        self.record("run_finished", **summary)
        with _lock:                      # no longer running: re-enable the fallback
            _running.discard(self)
            if _by_thread.get(threading.get_ident()) is self:
                del _by_thread[threading.get_ident()]
        return summary


def usage_fields(usage: Any) -> dict:
    """A crew's token usage as the provider reported it, field by field."""
    return {name: getattr(usage, name, 0) or 0 for name in USAGE_FIELDS}


def current() -> Optional[ClaimMonitor]:
    """The monitor for the claim now running in this context, if any.

    Resolves in the three steps described above _active_var: the context var,
    then the thread the request runs on (for a later flow step whose task never
    saw the context var), then the sole running claim (for an analyst thread
    CrewAI spawned that saw neither). Each step is leak-proof; see the notes."""
    monitor = _active_var.get()
    if monitor is not None:
        return monitor
    monitor = _by_thread.get(threading.get_ident())
    if monitor is not None:
        return monitor
    with _lock:
        return next(iter(_running)) if len(_running) == 1 else None


def on_step(step: Any) -> None:
    """Module-level step callback. CrewAI can checkpoint a crew only when its
    callbacks are plain named functions, not methods, so these forward to
    whichever claim is running in this context."""
    monitor = current()
    if monitor is not None:
        monitor.step_callback(step)


def on_task_done(output: Any) -> None:
    """Module-level task callback; see on_step."""
    monitor = current()
    if monitor is not None:
        monitor.task_callback(output)


def crew_callbacks(monitor: Optional[ClaimMonitor]) -> dict:
    """Keyword arguments to pass straight into Crew(...)."""
    if monitor is None:
        return {}
    return {"step_callback": on_step, "task_callback": on_task_done}


# ---------------------------------------------------------------- event bus
def register_listeners() -> None:
    """Hook CrewAI's event bus once per process. Handlers write to whichever
    claim is currently running."""
    global _listeners_registered
    if _listeners_registered:
        return
    try:
        from crewai.events import (
            LLMCallFailedEvent,
            ToolUsageErrorEvent,
            crewai_event_bus,
        )
    except ImportError:                                 # older CrewAI: callbacks still work
        _listeners_registered = True
        return

    @crewai_event_bus.on(LLMCallFailedEvent)
    def _llm_failed(source, event):
        monitor = current()
        if monitor is None:
            return
        monitor.counts["retried_llm_failures"] += 1
        message = str(getattr(event, "error", ""))
        low = message.lower()
        if "tokens per day" in low or "(tpd)" in low:
            kind = "daily_token_limit"
        elif "429" in message or "rate limit" in low:
            kind = "per_minute_rate_limit"
        elif "tool_use_failed" in message:
            kind = "malformed_tool_call"
        else:
            kind = "other"
        monitor.llm_error_kinds.add(kind)
        monitor.record("llm_call_failed", kind=kind, agent=getattr(event, "agent_role", None),
                       error=_short(message))

    @crewai_event_bus.on(ToolUsageErrorEvent)
    def _tool_error(source, event):
        monitor = current()
        if monitor is None:
            return
        monitor.counts["errors"] += 1
        monitor.record("tool_error", tool=getattr(event, "tool_name", None),
                       agent=getattr(event, "agent_role", None),
                       error=_short(str(getattr(event, "error", ""))))

    _listeners_registered = True
