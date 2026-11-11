"""Report-writing tools shared by the agent modes.

- agent_worker.py (MODE=agent): the tools run inside the agent's own tool loop.
- durable_tools_worker.py (MODE=durable-tools): the model only sees declaration-only copies;
  each call is executed as a durable activity via run_tool().
"""

import json
from typing import Annotated, Any

from agent_framework import FunctionTool, tool
from pydantic import Field

import report

RunId = Annotated[str, Field(description="The run_id given in the task.")]


# --------------------------------------------------------------------------- tools (run inside the agent entity)
@tool
def save_section(
    run_id: RunId,
    index: Annotated[int, Field(description="1-based section number.", ge=1)],
    title: Annotated[str, Field(description="Section title.")],
    markdown: Annotated[str, Field(description="Section body in Markdown, without a heading.")],
) -> str:
    """Save one report section. Saving the same index again overwrites it."""
    report.log_line(f"tool save_section({index}, {title!r})")
    report.maybe_crash(run_id, index)
    return report.write_section(run_id, index, title, markdown)


@tool
def list_sections(run_id: RunId) -> list[str]:
    """List the section files already saved for this run, in order. Empty for a fresh run."""
    names = sorted(p.name for p in report.run_dir(run_id).glob("[0-9][0-9]-*.md"))
    report.log_line(f"tool list_sections() -> {len(names)} saved")
    return names


@tool
def read_section(run_id: RunId, index: Annotated[int, Field(ge=1)]) -> str:
    """Read a saved section back."""
    report.log_line(f"tool read_section({index})")
    match = sorted(report.run_dir(run_id).glob(f"{index:02d}-*.md"))
    return match[0].read_text() if match else f"Section {index} has not been saved."


@tool
def save_report(
    run_id: RunId,
    title: Annotated[str, Field(description="Report title.")],
    summary: Annotated[str, Field(description="Executive summary, 4-6 Markdown bullets.")],
) -> str:
    """Assemble report.md from the summary and all saved sections. Returns its path."""
    report.log_line("tool save_report()")
    files = sorted(str(p) for p in report.run_dir(run_id).glob("[0-9][0-9]-*.md"))
    return report.write_report(run_id, title, summary, files)


TOOLS: list[FunctionTool] = [save_section, list_sections, read_section, save_report]
_BY_NAME = {t.name: t for t in TOOLS}


def declarations() -> list[FunctionTool]:
    """Declaration-only copies: same name, description and schema, but no implementation.

    The model can request them, but the agent's tool loop cannot run them, so it stops and hands
    the request back to the caller (the orchestrator), which runs it as a durable activity.
    """
    return [FunctionTool(name=t.name, description=t.description, func=None, input_model=t.parameters()) for t in TOOLS]


def call(name: str, arguments: str | dict[str, Any] | None) -> Any:
    """Run one tool by name with the model's JSON arguments."""
    args = json.loads(arguments) if isinstance(arguments, str) else (arguments or {})
    return _BY_NAME[name].func(**args)
