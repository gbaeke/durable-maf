"""Orchestration version: a Durable Task orchestrator written as plain Python code.

The orchestration plans a report, writes it section by section, saves each section
to disk and finally writes an executive summary. Every agent call and activity is
checkpointed in the Durable Task Scheduler, so if this process dies the orchestration
continues where it left off when a worker comes back -- completed LLM calls are not
repeated.

Failure injection: set CRASH_ON_SECTION=N to hard-kill the process (os._exit) while
saving section N. The crash happens once per run.

See workflow_worker.py for the same task built as a MAF workflow graph.
"""

import logging
import os
import signal
import threading
from collections.abc import Generator
from typing import Any, cast

from agent_framework import AgentResponse
from agent_framework_durabletask import DurableAIAgentOrchestrationContext, DurableAIAgentWorker
from durabletask.task import ActivityContext, OrchestrationContext, Task

import report
from common import GATEWAY_URL, MODEL, ORCHESTRATION_NAME, get_worker
from report import EDITOR, PLANNER, WRITER, Outline

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("worker")
# Durable Task worker logs are noisy at INFO
logging.getLogger("durabletask").setLevel(logging.WARNING)


# --------------------------------------------------------------------------- activities
def save_section(ctx: ActivityContext, data: dict[str, Any]) -> str:
    """Persist one section to disk. Optionally crashes the process to demo recovery."""
    report.maybe_crash(ctx.orchestration_id, data["index"])
    return report.write_section(ctx.orchestration_id, data["index"], data["title"], data["text"])


def save_report(ctx: ActivityContext, data: dict[str, Any]) -> str:
    return report.write_report(ctx.orchestration_id, data["title"], data["summary"], data["section_files"])


# --------------------------------------------------------------------------- orchestration
def research_report(context: OrchestrationContext, request: dict[str, Any]) -> Generator[Task[Any], Any, dict]:
    """Plan -> write N sections (each checkpointed) -> summarise -> save."""
    log = (lambda *a: logger.info(*a)) if not context.is_replaying else (lambda *a: None)
    agents = DurableAIAgentOrchestrationContext(context)
    planner, writer, editor = (agents.get_agent(n) for n in (PLANNER, WRITER, EDITOR))

    topic = request["topic"]
    n_sections = int(request.get("sections", 6))

    def status(phase: str, done: int = 0, total: int = n_sections, current: str = "") -> None:
        context.set_custom_status({"phase": phase, "sections_done": done, "sections_total": total, "current": current})

    # 1. Plan
    status("planning")
    log("[%s] planning report on %r", context.instance_id, topic)
    plan_raw: AgentResponse = yield planner.run(
        messages=report.plan_prompt(topic, n_sections),
        session=planner.create_session(),
        options={"response_format": Outline},
    )
    outline = cast(Outline, plan_raw.value)
    log("[%s] outline: %s", context.instance_id, [s.title for s in outline.sections])

    # 2. Write each section in one shared, durable writer session
    writer_session = writer.create_session()
    section_files: list[str] = []
    section_texts: list[str] = []  # rebuilt deterministically from recorded agent responses on replay
    for i, section in enumerate(outline.sections, start=1):
        status("writing", i - 1, len(outline.sections), section.title)
        log("[%s] writing section %d/%d: %s", context.instance_id, i, len(outline.sections), section.title)
        resp: AgentResponse = yield writer.run(messages=report.section_prompt(outline, i), session=writer_session)
        path = yield context.call_activity(save_section, input={"index": i, "title": section.title, "text": resp.text})
        section_files.append(path)
        section_texts.append(f"## {section.title}\n{resp.text}")
        log("[%s] saved section %d -> %s", context.instance_id, i, path)

    # 3. Executive summary
    status("summarising", len(outline.sections), len(outline.sections))
    summary: AgentResponse = yield editor.run(
        messages=report.summary_prompt("\n\n".join(section_texts)),
        session=editor.create_session(),
    )

    report_path = yield context.call_activity(
        save_report, input={"title": outline.title, "summary": summary.text, "section_files": section_files}
    )
    status("done", len(outline.sections), len(outline.sections))
    log("[%s] ✅ report written to %s", context.instance_id, report_path)
    return {"title": outline.title, "report": report_path, "sections": len(section_files)}


# --------------------------------------------------------------------------- main
def main() -> None:
    worker = get_worker(log_handler=logging.NullHandler())
    # GA 1.0 requires an explicit deployment strategy; this demo owns its task hub exclusively.
    agent_worker = DurableAIAgentWorker(worker, deployment_mode="isolated_v2")
    agent_worker.add_agent(report.make_agent(PLANNER, report.PLANNER_INSTRUCTIONS))
    agent_worker.add_agent(report.make_agent(WRITER, report.WRITER_INSTRUCTIONS))
    agent_worker.add_agent(report.make_agent(EDITOR, report.EDITOR_INSTRUCTIONS))
    worker.add_orchestrator(research_report)
    worker.add_activity(save_section)
    worker.add_activity(save_report)
    assert research_report.__name__ == ORCHESTRATION_NAME

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    with worker:
        worker.start()
        logger.info(
            "Orchestration worker pid=%d ready (model=%s via %s, crash on section=%s)",
            os.getpid(), MODEL, GATEWAY_URL, os.getenv("CRASH_ON_SECTION", "off"),
        )
        try:
            stop.wait()
        except KeyboardInterrupt:
            pass
    logger.info("Worker stopped")


if __name__ == "__main__":
    main()
