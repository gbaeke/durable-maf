"""Durable-tools version: the agent decides every step, and every model call and tool call is its own checkpoint.

This is the Dapr Agents style: the agent's tool loop is lifted out of the agent and into a durable
orchestration, so nothing the agent already did is repeated after a crash.

    tool_loop orchestrator                                  ToolAuthor agent (durable entity)
    ──────────────────────                                  ────────────────────────────────
    run(task) ───────────────────────────────────────────►  1 model call → "call list_sections"
    call_activity(run_tool: list_sections)   ← checkpoint
    run(tool result) ────────────────────────────────────►  1 model call → "call save_section(1)"
    call_activity(run_tool: save_section)    ← checkpoint
    ...                                                      ...
    run(tool result) ────────────────────────────────────►  1 model call → final answer (no calls) → done

How: the agent is given declaration-only copies of the tools (name, description, schema, no code).
The model can ask for them, but the agent can't run them, so each agent call returns after exactly
one model call with the requested tool calls. The orchestrator runs each one as an activity and
sends the results back. The loop knows nothing about reports: the agent chooses which tools to
call, in what order, and when it is finished.

Responses API (store=False), same tools, files and crash injection as the agent version.
"""

import logging
import os
import signal
import threading
from collections.abc import Generator
from typing import Any

from agent_framework import AgentResponse, Content, Message
from agent_framework_durabletask import DurableAIAgentOrchestrationContext, DurableAIAgentWorker

# The durable agent only takes text as `messages`; tool results go in as `context_messages`,
# serialized the way the extension's own workflow host does it.
from agent_framework_durabletask._response_utils import serialize_input_message
from durabletask import task
from durabletask.task import ActivityContext, OrchestrationContext, Task

import report
import report_tools
from common import DURABLE_TOOLS_NAME, GATEWAY_URL, MODEL, get_worker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("durable-tools-worker")
logging.getLogger("durabletask").setLevel(logging.WARNING)

TOOL_AUTHOR = "ToolAuthor"
MAX_STEPS = 60  # model calls per run; a safety limit, the agent normally stops long before

TOOL_AUTHOR_INSTRUCTIONS = """\
You are an autonomous technical author. You write a complete report in Markdown and save it with your tools.

1. Decide the title and outline yourself, then write and save the sections one by one with save_section
   (150-250 words each, no top-level heading, consistent with earlier sections). Save one section per tool call.
2. When every section is saved, write a 4-6 bullet executive summary and call save_report.
3. Only then reply with a short final message (no tool calls). You decide when the work is complete.

Always pass the run_id you were given to every tool.
"""


# --------------------------------------------------------------------------- activity: one tool call
def run_tool(ctx: ActivityContext, call: dict[str, Any]) -> dict[str, Any]:
    """Execute one tool call the model asked for. Errors go back to the model as the tool result."""
    try:
        return {"call_id": call["call_id"], "result": report_tools.call(call["name"], call["arguments"])}
    except Exception as exc:  # noqa: BLE001 - the model gets to see and handle tool errors
        return {"call_id": call["call_id"], "error": f"{type(exc).__name__}: {exc}"}


# --------------------------------------------------------------------------- orchestration: the tool loop
def tool_loop(context: OrchestrationContext, request: dict[str, Any]) -> Generator[Task[Any], Any, dict]:
    """Generic durable tool loop: model call -> tool activities -> model call ... until the model stops."""
    log = (lambda *a: logger.info(*a)) if not context.is_replaying else (lambda *a: None)
    run_id = context.instance_id
    author = DurableAIAgentOrchestrationContext(context).get_agent(TOOL_AUTHOR)
    session = author.create_session()

    task_text = (
        f"Task: write a report on: {request['topic']}\n"
        f"Aim for about {int(request.get('sections', 6))} sections.\nrun_id: {run_id}"
    )
    tool_results: Message | None = None  # results of the previous step's tool calls
    sections_saved, report_path, last_tool, final_text = 0, None, "", ""

    for step in range(1, MAX_STEPS + 1):
        context.set_custom_status({
            "phase": f"step {step}", "sections_done": sections_saved, "sections_total": "?", "current": last_tool,
        })
        # One durable agent call = exactly one model call (the tools are declaration-only).
        if tool_results is None:
            resp: AgentResponse = yield author.run(messages=task_text, session=session)
        else:
            resp = yield author.run(
                messages="",  # text preview only; the real input is the tool-result message below
                session=session,
                context_messages=[serialize_input_message(tool_results)],
                context_message_ids=[f"{run_id}:tool-results:{step}"],  # never stored twice on redelivery
            )
        calls = [c for m in resp.messages for c in m.contents if c.type == "function_call"]
        if not calls:  # the agent decided it is done
            final_text = resp.text
            break

        log("[%s] step %d: model asks for %s", run_id, step, [c.name for c in calls])
        # Each tool call is its own activity = its own checkpoint. Parallel calls run in parallel.
        results: list[dict[str, Any]] = yield task.when_all([
            context.call_activity(
                run_tool, input={"call_id": c.call_id, "name": c.name, "arguments": c.arguments}
            )
            for c in calls
        ])
        for c, r in zip(calls, results, strict=True):
            last_tool = c.name
            if c.name == "save_section" and "error" not in r:
                sections_saved += 1
            if c.name == "save_report" and "error" not in r:
                report_path = r["result"]

        tool_results = Message(
            role="tool",
            contents=[
                Content.from_function_result(r["call_id"], result=r.get("result"), exception=r.get("error"))
                for r in results
            ],
        )
    else:
        raise RuntimeError(f"{TOOL_AUTHOR} did not finish within {MAX_STEPS} model calls")

    context.set_custom_status({
        "phase": "done", "sections_done": sections_saved, "sections_total": sections_saved, "current": "",
    })
    log("[%s] ✅ agent finished after %d model calls", run_id, step)
    return {"report": report_path, "sections": sections_saved, "model_calls": step, "final": final_text}


# --------------------------------------------------------------------------- main
def main() -> None:
    worker = get_worker(log_handler=logging.NullHandler())
    agent_worker = DurableAIAgentWorker(worker, deployment_mode="isolated_v2")
    agent_worker.add_agent(
        report.make_agent(TOOL_AUTHOR, TOOL_AUTHOR_INSTRUCTIONS, tools=report_tools.declarations(), api="responses")
    )
    worker.add_orchestrator(tool_loop)
    worker.add_activity(run_tool)
    assert tool_loop.__name__ == DURABLE_TOOLS_NAME

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    with worker:
        worker.start()
        logger.info(
            "Durable-tools worker pid=%d ready (Responses API, model=%s via %s, crash on section=%s)",
            os.getpid(), MODEL, GATEWAY_URL, os.getenv("CRASH_ON_SECTION", "off"),
        )
        try:
            stop.wait()
        except KeyboardInterrupt:
            pass
    logger.info("Worker stopped")


if __name__ == "__main__":
    main()
