"""Client CLI for the durable report agent (both versions).

  uv run client.py start "topic" [--sections 6] [--mode orchestration|workflow|agent|durable-tools]
                                                  start a new run, prints the instance id
  uv run client.py status <instance-id>           show runtime status + progress
  uv run client.py wait <instance-id>             follow progress until it finishes
"""

import argparse
import json
import sys
import time
import uuid

from agent_framework_durabletask import DurableWorkflowClient
from durabletask.client import OrchestrationStatus

from common import AGENT_LOOP_NAME, DURABLE_TOOLS_NAME, ORCHESTRATION_NAME, WORKFLOW_NAME, get_client

MODES = ("orchestration", "workflow", "agent", "durable-tools")


def start(topic: str, sections: int, mode: str) -> None:
    client = get_client()
    if mode != "workflow":
        name = {"orchestration": ORCHESTRATION_NAME, "agent": AGENT_LOOP_NAME, "durable-tools": DURABLE_TOOLS_NAME}[mode]
        instance_id = client.schedule_new_orchestration(name, input={"topic": topic, "sections": sections})
    else:
        # We pick the instance id ourselves and pass it in as run_id: workflow executors use it
        # to name the output folder, just like activities use ctx.orchestration_id in worker.py.
        run_id = uuid.uuid4().hex
        instance_id = DurableWorkflowClient(client, workflow_name=WORKFLOW_NAME).start_workflow(
            input={"topic": topic, "sections": sections, "run_id": run_id}, instance_id=run_id
        )
    print(instance_id)


def _show(state) -> str:
    progress = json.loads(state.serialized_custom_status) if state.serialized_custom_status else {}
    line = f"{state.runtime_status.name:<10}"
    if progress.get("phase") == "agent working":  # agent version: progress is what its tools saved
        from common import OUTPUT_DIR
        saved = len(list((OUTPUT_DIR / progress["run_id"]).glob("[0-9][0-9]-*.md")))
        line += f" {'agent':<11} {saved} sections on disk"
    elif "phase" in progress and "sections_done" in progress:  # orchestration version: our own status
        line += f" {progress['phase']:<11} {progress.get('sections_done', 0)}/{progress.get('sections_total', '?')}"
        if progress.get("current"):
            line += f"  ▶ {progress['current']}"
    elif progress.get("events"):  # workflow version: the host publishes executor events
        events = progress["events"]
        saved = sum(1 for e in events if e.get("type") == "executor_completed" and e.get("executor_id") == "section_saver")
        invoked = [e["executor_id"] for e in events if e.get("type") == "executor_invoked"]
        line += f" {'step':<11} {saved} saved  ▶ {invoked[-1] if invoked else '?'}"
    return line


def _output(state) -> str:
    out = json.loads(state.serialized_output)
    if isinstance(out, list):  # workflow outputs come back as a list of yielded values
        out = out[-1] if out else None
    return json.dumps(out, indent=2)


def status(instance_id: str) -> None:
    state = get_client().get_orchestration_state(instance_id)
    if not state:
        sys.exit(f"No orchestration {instance_id}")
    print(_show(state))
    if state.serialized_output:
        print(_output(state))


def wait(instance_id: str, timeout: int) -> None:
    client = get_client()
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        state = client.get_orchestration_state(instance_id)
        line = _show(state) if state else "PENDING"
        if line != last:
            print(f"{time.strftime('%H:%M:%S')}  {line}", flush=True)
            last = line
        if state and state.runtime_status in (
            OrchestrationStatus.COMPLETED, OrchestrationStatus.FAILED, OrchestrationStatus.TERMINATED
        ):
            if state.serialized_output:
                print(_output(state))
            if state.failure_details:
                print(state.failure_details.message)
            sys.exit(0 if state.runtime_status == OrchestrationStatus.COMPLETED else 1)
        time.sleep(1)
    sys.exit("Timed out")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start")
    s.add_argument("topic")
    s.add_argument("--sections", type=int, default=6)
    s.add_argument("--mode", choices=MODES, default="orchestration")
    sub.add_parser("status").add_argument("instance_id")
    w = sub.add_parser("wait")
    w.add_argument("instance_id")
    w.add_argument("--timeout", type=int, default=1800)
    a = p.parse_args()
    if a.cmd == "start":
        start(a.topic, a.sections, a.mode)
    elif a.cmd == "status":
        status(a.instance_id)
    else:
        wait(a.instance_id, a.timeout)


if __name__ == "__main__":
    main()
