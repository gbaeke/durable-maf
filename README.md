# durable-maf

A durable agent in Microsoft Agent Framework (Python, `agent-framework-durabletask` 1.0.0 GA) that
runs a long multi-step task and **survives a hard crash of its worker process**.

- **Durable state:** local [Durable Task Scheduler emulator](https://learn.microsoft.com/azure/azure-functions/durable/durable-task-scheduler/develop-with-durable-task-scheduler) (Docker), no Azure needed
- **LLM:** Luna through the local [agentgateway](https://agentgateway.dev) on `localhost:4000`
- **Hosting:** self-hosted worker process (bring-your-own-compute model)

## What it does

`research_report` orchestration:

1. **Planner** agent writes an outline (structured output → `Outline` model)
2. **Writer** agent writes each section in one shared durable session (keeps context across sections)
3. `save_section` activity writes each section to `output/<instance>/NN-*.md`
4. **Editor** agent writes an executive summary, `save_report` writes `report.md`

Each agent call and activity result is checkpointed in the scheduler. When a worker dies, the
orchestration waits; the next worker replays the history (returning recorded LLM results instead of
calling the model again) and continues with the first unfinished step.

```
worker #1  plan ✓  s1 ✓  s2 ✓  s3 ✓(LLM) → save s3 💥 os._exit(137)
worker #2  (replay plan, s1–s3 from history)  save s3 ✓  s4 ✓  s5 ✓  s6 ✓  summary ✓  report ✓
```

## Two versions of the same task

| `MODE` | Worker | Built as |
|---|---|---|
| `orchestration` (default) | `worker.py` | Durable Task orchestrator: the flow is a Python `for` loop with `yield`s |
| `workflow` | `workflow_worker.py` | MAF workflow graph, hosted durably with `DurableAIAgentWorker.configure_workflow()` |
| `agent` | `agent_worker.py` | One agent with tools decides every step; it runs its own tool loop inside **one** durable message |
| `durable-tools` | `durable_tools_worker.py` | Same agent and tools, but every model call and every tool call is its own checkpoint (Dapr Agents style) |

The first two have fixed steps written by us. In the two agent modes the agent picks the outline,
the order of work and when it is done. Both agent modes use the **Responses API**: Luna rejects
function tools on `/v1/chat/completions` (`Function tools with reasoning_effort are not supported`).

What a crash on section 3 costs (measured):

| Mode | Repeated after the crash |
|---|---|
| `orchestration`, `workflow` | only the interrupted save step |
| `agent` | the whole agent run restarts; it re-reads its saved files with `list_sections` to continue (12 model calls instead of 8) |
| `durable-tools` | only the interrupted `save_section` tool call; no model call repeated |

The workflow graph:

```
intake ──► Planner ──► dispatcher ──► Writer ──► section_saver ──► Editor ──► finisher
                                        ▲              │
                                        └─next section─┘
```

Agent nodes (Planner, Writer, Editor) become durable entities; the other nodes become activities.
Run-wide data (run id, outline, file paths) lives in workflow state (`ctx.set_state` / `get_state`),
which the durable host checkpoints after every step. Both versions share agents, prompts, file
helpers and crash injection in `report.py`.

## Run the demo

```bash
uv sync
./demo.sh                          # orchestration version: crash on section 3 of 6, then recover
MODE=workflow ./demo.sh            # same demo, workflow version
MODE=agent ./demo.sh               # one agent with tools, its own tool loop
MODE=durable-tools ./demo.sh       # one agent with tools, every model/tool call checkpointed
CRASH_ON=5 SECTIONS=8 MODE=workflow ./demo.sh "Your topic here"
```

`demo.sh` starts agentgateway and the emulator if they aren't running, starts a worker with
`CRASH_ON_SECTION`, kicks off an orchestration, waits for the crash, starts a fresh worker and
follows the run to completion. At the end it prints `output/llm_calls.log`: every **real** model
call with the worker pid — the pid changes once at the crash and no call is repeated.

Watch it live in the dashboard: <http://localhost:8082> → task hub `durable-maf`
(enable *Agent pages* under settings → Preview features to see agent sessions).

## Run it by hand (kill it yourself)

```bash
# infra
docker run -d --name dts-emulator -e DTS_TASK_HUB_NAMES=durable-maf \
  -p 8080:8080 -p 8082:8082 mcr.microsoft.com/dts/dts-emulator:latest
agentgateway -f ~/.config/agentgateway/config.yaml &

uv run worker.py                                   # terminal 1
ID=$(uv run client.py start "Some topic" --sections 8)   # terminal 2
uv run client.py wait $ID
# Ctrl+C / kill -9 the worker mid-run, start it again: the run continues.
```

## Files

| File | Purpose |
|---|---|
| `report.py` | Shared: agents, prompts, `Outline` model, file helpers, crash injection, LLM-call logging |
| `worker.py` | Orchestration version: orchestrator + activities |
| `workflow_worker.py` | Workflow version: executors + graph |
| `report_tools.py` | Tools for the agent modes, plus declaration-only copies and `call()` for durable-tools |
| `agent_worker.py` | Agent version: one agent, real tools, one durable message |
| `durable_tools_worker.py` | Durable-tools version: generic tool-loop orchestrator + `run_tool` activity |
| `client.py` | `start [--mode]` / `status` / `wait` CLI for both versions |
| `common.py` | Emulator + gateway settings (from `.env`) |
| `demo.sh` | End-to-end crash/recovery demo |

## Configuration (`.env`)

| Variable | Default | |
|---|---|---|
| `DTS_ENDPOINT` | `http://localhost:8080` | emulator gRPC endpoint |
| `TASKHUB` | `durable-maf` | must exist in the emulator (`DTS_TASK_HUB_NAMES`) |
| `GATEWAY_URL` | `http://localhost:4000/v1` | agentgateway, OpenAI-compatible |
| `MODEL` | `azure-gpt-6-luna` | gateway route; `gpt-6-luna` once its OpenAI key is renewed |

## Notes

- GA 1.0 requires an explicit deployment strategy: the worker uses
  `deployment_mode="isolated_v2"`, which assumes this task hub is used only by this app.
- Orchestrator code must be deterministic — it's replayed on recovery. All I/O (file writes,
  LLM calls) happens in activities or agent entities; section texts are rebuilt from recorded
  agent responses rather than read from disk.
- Durable-tools version: the durable agent accepts only text as `messages`, so tool results are sent
  as `context_messages`, serialized with `serialize_input_message` from the extension's private
  `_response_utils` module (the same function its workflow host uses). Pin the package version.
- Workflow version: the durable host publishes an accumulating event log (with step payloads) as
  the orchestration's custom status. It grows with the report and counts against the 1 MB limit.
- Section text passes through the scheduler, which has a 1 MB limit per item. Fine for this
  demo, but it won't scale as written. See [docs/payload-limits.md](docs/payload-limits.md).
- Durability is at-least-once per step: a step that was in flight when the process died is run
  again (here `save_section` for section 3). Keep activities idempotent.
