"""Agent version: one agent with tools gets the task once and decides every step, including when it is done.

    client ──► agent_loop orchestrator ──(one message)──► Author agent (durable entity)
                                                           model ─► tool ─► model ─► tool ─► … ─► final answer
                                                           (the agent's own tool loop; it stops when it decides)

- No Planner/Writer/Editor and no hardcoded steps or turns: the agent picks the outline, writes and
  saves sections, writes the summary, saves the report and then answers. Nothing calls it again.
- The orchestrator exists only to give the run an id, a status and a durable result. It sends
  exactly one message to the agent entity.
- Durability is per message: the agent's tool loop is NOT checkpointed. If the worker dies mid-run,
  the scheduler hands the same message to the next worker and the agent starts its loop again.
  It then calls list_sections first, sees what is already on disk and continues from there --
  recovery driven by the agent, using its tools' side effects as progress. Tools are idempotent.

Uses the Responses API (OpenAIChatClient) with store=False, so the durable session is the only record
of the conversation. Same gateway, model, file layout and crash injection as the other versions.
"""

import logging
import os
import signal
import threading
from collections.abc import Generator
from typing import Any, cast

from agent_framework import AgentResponse
from agent_framework_durabletask import DurableAIAgentOrchestrationContext, DurableAIAgentWorker
from durabletask.task import OrchestrationContext, Task
from pydantic import BaseModel, Field

import report
from common import AGENT_LOOP_NAME, GATEWAY_URL, MODEL, get_worker
from report_tools import TOOLS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("agent-worker")
logging.getLogger("durabletask").setLevel(logging.WARNING)

AUTHOR = "Author"

AUTHOR_INSTRUCTIONS = """\
You are an autonomous technical author. You write a complete report in Markdown and save it with your tools.

1. Always start by calling list_sections. You may have been interrupted before; if sections already exist,
   read the last one, keep the same outline and continue with the next section. Never rewrite saved sections.
2. Decide the title and outline yourself, then write and save the sections one by one with save_section
   (150-250 words each, no top-level heading, consistent with earlier sections).
3. When every section is saved, write a 4-6 bullet executive summary and call save_report.
4. Only then give your final answer. You decide when the work is complete.

Always pass the run_id you were given to every tool.
"""

class FinalAnswer(BaseModel):
    """The agent's final answer, given once it has decided the work is complete."""

    title: str
    report_path: str = Field(description="Path returned by save_report.")
    sections: int = Field(description="Number of sections in the report.")
    note: str = Field(description="One sentence on how the work went.")


# --------------------------------------------------------------------------- orchestration
def agent_loop(context: OrchestrationContext, request: dict[str, Any]) -> Generator[Task[Any], Any, dict]:
    """Hand the whole task to the Author agent in a single message and wait for its final answer."""
    run_id = context.instance_id
    author = DurableAIAgentOrchestrationContext(context).get_agent(AUTHOR)
    context.set_custom_status({"phase": "agent working", "run_id": run_id})
    if not context.is_replaying:
        logger.info("[%s] handing the task to %s", run_id, AUTHOR)

    resp: AgentResponse = yield author.run(
        messages=(
            f"Task: write a report on: {request['topic']}\n"
            f"Aim for about {int(request.get('sections', 6))} sections.\nrun_id: {run_id}"
        ),
        session=author.create_session(),
        options={"response_format": FinalAnswer},
    )
    answer = cast(FinalAnswer | None, resp.value)
    if answer is None:  # the agent call failed (e.g. a 400 from the model) or gave no structured answer
        context.set_custom_status({"phase": "failed", "run_id": run_id})
        raise RuntimeError(f"{AUTHOR} gave no final answer. Its last reply: {resp.text[:500]!r}")
    context.set_custom_status({"phase": "done", "run_id": run_id})
    if not context.is_replaying:
        logger.info("[%s] ✅ agent finished: %s", run_id, answer.model_dump())
    return {"title": answer.title, "report": answer.report_path, "sections": answer.sections, "note": answer.note}


# --------------------------------------------------------------------------- main
def main() -> None:
    worker = get_worker(log_handler=logging.NullHandler())
    agent_worker = DurableAIAgentWorker(worker, deployment_mode="isolated_v2")
    agent_worker.add_agent(
        report.make_agent(
            AUTHOR,
            AUTHOR_INSTRUCTIONS,
            tools=TOOLS,
            api="responses",
            max_tool_calls=60,  # safety limit on the agent's own loop
        )
    )
    worker.add_orchestrator(agent_loop)
    assert agent_loop.__name__ == AGENT_LOOP_NAME

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    with worker:
        worker.start()
        logger.info(
            "Agent worker pid=%d ready (Responses API, model=%s via %s, crash on section=%s)",
            os.getpid(), MODEL, GATEWAY_URL, os.getenv("CRASH_ON_SECTION", "off"),
        )
        try:
            stop.wait()
        except KeyboardInterrupt:
            pass
    logger.info("Worker stopped")


if __name__ == "__main__":
    main()
