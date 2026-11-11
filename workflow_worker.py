"""Workflow version: the same report task built as a Microsoft Agent Framework workflow graph.

    intake ──► Planner ──► dispatcher ──► Writer ──► section_saver ──► Editor ──► finisher
                                            ▲              │
                                            └──next section┘

- Agent executors (Planner, Writer, Editor) become durable entities: one conversation per run,
  so the Writer remembers earlier sections across loop iterations.
- Non-agent executors (intake, dispatcher, section_saver, finisher) become durable activities.
- Run-wide data (run id, outline, saved file paths) lives in workflow state, which the durable
  host carries between steps and checkpoints after every step.

DurableAIAgentWorker.configure_workflow() registers all of that plus the orchestrator that drives
the graph (named dafx-report_workflow). Same agents, prompts and crash injection as worker.py.
"""

import logging
import os
import signal
import threading
from pathlib import Path
from typing import Any, Never

from agent_framework import AgentExecutorResponse, Executor, Workflow, WorkflowBuilder, WorkflowContext, handler
from agent_framework_durabletask import DurableAIAgentWorker

import report
from common import GATEWAY_URL, MODEL, WORKFLOW_NAME, get_worker
from report import EDITOR, PLANNER, WRITER, Outline

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("workflow-worker")
logging.getLogger("durabletask").setLevel(logging.WARNING)


# --------------------------------------------------------------------------- executors (run as activities)
class Intake(Executor):
    """Start node: remember the run, then ask the Planner for an outline."""

    @handler
    async def start(self, request: dict[str, Any], ctx: WorkflowContext[str]) -> None:
        ctx.set_state("run_id", request["run_id"])
        ctx.set_state("section_files", [])
        logger.info("[%s] planning report on %r", request["run_id"], request["topic"])
        await ctx.send_message(report.plan_prompt(request["topic"], int(request.get("sections", 6))))


class Dispatcher(Executor):
    """Parse the Planner's structured outline and send section 1 to the Writer."""

    @handler
    async def on_outline(self, response: AgentExecutorResponse, ctx: WorkflowContext[str]) -> None:
        outline = Outline.model_validate_json(response.agent_response.text)
        ctx.set_state("outline", outline.model_dump())
        logger.info("outline: %s", [s.title for s in outline.sections])
        await ctx.send_message(report.section_prompt(outline, 1))


class SectionSaver(Executor):
    """Save the section the Writer just produced, then loop back or move on to the Editor."""

    @handler
    async def on_section(self, response: AgentExecutorResponse, ctx: WorkflowContext[str]) -> None:
        run_id = ctx.get_state("run_id")
        outline = Outline.model_validate(ctx.get_state("outline"))
        files: list[str] = list(ctx.get_state("section_files"))
        i = len(files) + 1  # state is only committed when this step completes, so a retry sees the same i

        report.maybe_crash(run_id, i)
        files.append(report.write_section(run_id, i, outline.sections[i - 1].title, response.agent_response.text))
        ctx.set_state("section_files", files)
        logger.info("[%s] saved section %d/%d", run_id, i, len(outline.sections))

        if i < len(outline.sections):
            await ctx.send_message(report.section_prompt(outline, i + 1), target_id=WRITER)
        else:
            full_text = "\n\n".join(Path(p).read_text() for p in files)
            await ctx.send_message(report.summary_prompt(full_text), target_id=EDITOR)


class Finisher(Executor):
    """Write report.md and yield the workflow output."""

    @handler
    async def on_summary(self, response: AgentExecutorResponse, ctx: WorkflowContext[Never, dict]) -> None:
        run_id = ctx.get_state("run_id")
        outline = Outline.model_validate(ctx.get_state("outline"))
        files: list[str] = ctx.get_state("section_files")
        path = report.write_report(run_id, outline.title, response.agent_response.text, files)
        logger.info("[%s] ✅ report written to %s", run_id, path)
        await ctx.yield_output({"title": outline.title, "report": path, "sections": len(files)})


# --------------------------------------------------------------------------- graph
def create_workflow() -> Workflow:
    planner = report.make_agent(PLANNER, report.PLANNER_INSTRUCTIONS, response_format=Outline)
    writer = report.make_agent(WRITER, report.WRITER_INSTRUCTIONS)
    editor = report.make_agent(EDITOR, report.EDITOR_INSTRUCTIONS)
    intake = Intake(id="intake")
    dispatcher = Dispatcher(id="dispatcher")
    saver = SectionSaver(id="section_saver")
    finisher = Finisher(id="finisher")

    return (
        WorkflowBuilder(
            name=WORKFLOW_NAME,
            start_executor=intake,
            max_iterations=200,  # each section takes 2 supersteps (Writer + section_saver)
            output_from=[finisher],  # only the finisher's yield_output is the workflow result
        )
        .add_edge(intake, planner)
        .add_edge(planner, dispatcher)
        .add_edge(dispatcher, writer)
        .add_edge(writer, saver)
        .add_edge(saver, writer)  # loop: next section
        .add_edge(saver, editor)  # exit: all sections done
        .add_edge(editor, finisher)
        .build()
    )


# --------------------------------------------------------------------------- main
def main() -> None:
    worker = get_worker(log_handler=logging.NullHandler())
    agent_worker = DurableAIAgentWorker(worker, deployment_mode="isolated_v2")
    workflow = create_workflow()
    agent_worker.configure_workflow(workflow)

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    with worker:
        worker.start()
        logger.info(
            "Workflow worker pid=%d ready: %s with executors %s (model=%s via %s, crash on section=%s)",
            os.getpid(), WORKFLOW_NAME, list(workflow.executors), MODEL, GATEWAY_URL,
            os.getenv("CRASH_ON_SECTION", "off"),
        )
        try:
            stop.wait()
        except KeyboardInterrupt:
            pass
    logger.info("Worker stopped")


if __name__ == "__main__":
    main()
