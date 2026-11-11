"""Shared building blocks for both versions of the report agent.

- the three agents (Planner, Writer, Editor) calling Luna through agentgateway
- the Outline model used for structured planner output
- file helpers that write sections and the final report under output/<run-id>/
- crash injection (CRASH_ON_SECTION) used to demo recovery

Used by worker.py (Durable Task orchestration) and workflow_worker.py (MAF workflow).
"""

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_framework import Agent, ChatContext, chat_middleware
from agent_framework.openai import OpenAIChatClient, OpenAIChatCompletionClient
from pydantic import BaseModel

from common import GATEWAY_URL, MODEL, OUTPUT_DIR

logger = logging.getLogger("report")

PLANNER, WRITER, EDITOR = "Planner", "Writer", "Editor"
LLM_LOG = OUTPUT_DIR / "llm_calls.log"

PLANNER_INSTRUCTIONS = (
    "You plan technical reports. Produce a title and a list of sections, each with a "
    "short title and a one or two sentence brief of what it must cover."
)
WRITER_INSTRUCTIONS = (
    "You are a precise technical writer. Write the requested report section in Markdown "
    "(150-250 words, no top-level heading). Stay consistent with sections you wrote earlier "
    "in this conversation and do not repeat them."
)
EDITOR_INSTRUCTIONS = "You are an editor. Given a full report, write a crisp executive summary of 4-6 bullet points."


# --------------------------------------------------------------------------- models
class Section(BaseModel):
    title: str
    brief: str


class Outline(BaseModel):
    title: str
    sections: list[Section]


# --------------------------------------------------------------------------- prompts
def plan_prompt(topic: str, n_sections: int) -> str:
    return f"Plan a report on: {topic}. Use exactly {n_sections} sections."


def section_prompt(outline: Outline, i: int) -> str:
    s = outline.sections[i - 1]
    return f"Report: {outline.title}\nWrite section {i}: {s.title}\nBrief: {s.brief}"


def summary_prompt(full_text: str) -> str:
    return f"Write the executive summary for this report:\n\n{full_text}"


# --------------------------------------------------------------------------- agents
@chat_middleware
async def log_llm_calls(context: ChatContext, call_next) -> None:
    """Record every real model call, so you can verify nothing is re-run after a crash."""
    last_msg = context.messages[-1] if context.messages else None
    last = last_msg.text if last_msg else ""
    if last_msg is not None and str(last_msg.role) == "tool":
        label = "(model call after tool result)"  # agent mode: the tool loop inside one turn
    else:
        # Show the most telling line of the prompt (e.g. "Write section 3: ...")
        label = next((ln for ln in last.splitlines() if ln.startswith("Write section")), last.splitlines()[0] if last else "")
    await call_next()
    log_line(label)


def log_line(label: str) -> None:
    """Append one line to output/llm_calls.log (model calls, and tool calls in agent mode)."""
    line = f"{datetime.now():%H:%M:%S} pid={os.getpid()}  {label[:100]}"
    LLM_LOG.parent.mkdir(parents=True, exist_ok=True)
    with LLM_LOG.open("a") as f:
        f.write(line + "\n")
    logger.info("%s", line)


def make_agent(
    name: str, instructions: str, tools: list[Any] | None = None, api: str = "chat", **options: Any
) -> Agent:
    """Build an agent that calls Luna through agentgateway.

    api="chat"       Chat Completions (/v1/chat/completions)
    api="responses"  Responses API (/v1/responses), with store=False so the durable session -- not the
                     provider -- holds the conversation (no server-side state to get out of step on retry).
    """
    client_cls = {"chat": OpenAIChatCompletionClient, "responses": OpenAIChatClient}[api]
    client = client_cls(
        model=MODEL,
        base_url=GATEWAY_URL,
        api_key=os.getenv("GATEWAY_API_KEY", "unused"),  # agentgateway holds the real provider key
    )
    if api == "responses":
        options.setdefault("store", False)
    return Agent(
        client=client,
        name=name,
        instructions=instructions,
        tools=tools,
        middleware=[log_llm_calls],
        default_options=options or None,
    )


# --------------------------------------------------------------------------- files
def run_dir(run_id: str) -> Path:
    path = OUTPUT_DIR / run_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def maybe_crash(run_id: str, index: int) -> None:
    """Hard-kill the process while saving section CRASH_ON_SECTION, once per run."""
    crash_on = int(os.getenv("CRASH_ON_SECTION", "0"))
    marker = run_dir(run_id) / ".crashed"
    if index == crash_on and not marker.exists():
        marker.touch()
        logger.error("💥 Simulating a hard crash while saving section %d (os._exit)", index)
        os._exit(137)


def write_section(run_id: str, index: int, title: str, text: str) -> str:
    """Write one section file; overwrites, so running it twice is safe."""
    title = title.strip().removeprefix(f"{index}.").removeprefix(f"{index})").strip()  # agents sometimes number titles
    body = text.strip()
    if body.startswith("#"):  # the model sometimes adds its own heading; we add a consistent one
        body = body.split("\n", 1)[-1].strip()
    path = run_dir(run_id) / f"{index:02d}-{_slug(title)}.md"
    path.write_text(f"## {index}. {title}\n\n{body}\n")
    return str(path)


def write_report(run_id: str, title: str, summary: str, section_files: list[str]) -> str:
    parts = [f"# {title}\n", "## Executive summary\n", summary, ""]
    parts += [Path(p).read_text() for p in section_files]
    path = run_dir(run_id) / "report.md"
    path.write_text("\n".join(parts))
    return str(path)


def _slug(s: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in s.lower()).strip("-")[:40]
