# Payload size limits in the Durable Task Scheduler

Status: **known limitation of this demo, not fixed.** The demo is well within the limits
(a 6-section report is about 10 KB). This page explains where it would break at scale and
how to change it.

## The limits

From the [Durable Task Scheduler docs](https://learn.microsoft.com/azure/durable-task/scheduler/durable-task-scheduler#limitations-and-considerations):

| Data | Max size |
|---|---|
| Orchestration inputs and outputs | 1 MB |
| Activity inputs and outputs | 1 MB |
| External event data | 1 MB |
| Orchestration custom status | 1 MB |
| Entity state (durable agent sessions are entities) | 1 MB |
| Everything one step sends back to the scheduler at once (work-item completion) | 3.9 MB |

`agent-framework-durabletask` hardcodes `DTS_MAX_STATE_BYTES = 1_048_576`. It can trim a
session's history when it reaches 85% of that, but by default it keeps the full history.

Besides the hard limits, everything in an orchestration's history is loaded again on every
replay. Large histories make recovery slower and use more memory, even below 1 MB per item.

## Where text goes through the scheduler today

The final report is written to disk; the orchestration only returns file paths (about 200
bytes). The section text, however, passes through the scheduler several times. Measured from a
demo run: about 1.5 KB per section, about 10 KB for the full report.

| Data | Stored in | Grows with | Risk |
|---|---|---|---|
| Each section the writer returns | Orchestration history (recorded result) | Number of sections | Low per item; history keeps growing |
| `save_section` input (section text) | Activity input | Section size | Low |
| **Writer session** (all prompts and sections) | **One entity, 1 MB cap** | Every section, cumulatively | **Hits the limit first** |
| **Editor prompt** (full report text) | Entity operation input | Report size | **Second to fail** |
| Final report | Local disk (`output/`) | — | None for DTS |

**Workflow version (`MODE=workflow`) adds two more:**

| Data | Stored in | Grows with | Risk |
|---|---|---|---|
| Workflow state (run id, outline, file paths) | Orchestration history, passed to every activity step | Number of sections (paths only) | Low |
| **Event log** published by the durable host (`executor_invoked` / `executor_completed` with their data) | **Orchestration custom status, 1 MB cap** | Every step, including each section's text | **High: fails before the writer session does** |

The standalone durable host has no public switch to turn the event log off (the Azure Functions
host leaves it out because its custom status is capped at 16 KB).

Rough breaking point: a few hundred sections, or a few hundred KB of report text.

## How to fix it (claim-check pattern)

1. **Store text outside DTS, pass references through it.** An activity calls the writer and
   saves the result to blob storage, then returns only a reference. Locally, use Azurite next to
   the DTS emulator. Local disk only works while every worker runs on the same machine.
2. **Give the editor references, not text.** Either give the editor a tool that reads sections
   from storage, or have an activity build a condensed digest first.
3. **Bound the writer session.** Either use a fresh session per section and pass the outline and
   short summaries of earlier sections, or keep one session and enable history trimming in the
   package (Python: retention and state budgets; .NET: `DurableAgentHistoryRetentionMode.Auto`).
4. **Safety net, not the design:** large payload support in the Durable Task SDK
   (`pip install durabletask[azure-blob-payloads]`, preview). Payloads above a threshold
   (default 256 KB, max 1 MB) are gzipped and stored in blob storage automatically; clients and
   workers must use the same store. The Python package can also offload agent session state to
   blob storage. Both work with the DTS emulator plus Azurite.

### Trade-offs of moving the writer into an activity

- If the worker crashes during the LLM call, that one call runs again (activities run at least
  once).
- The dashboard's agent-session view no longer shows the writer's conversation.

## References

- [Durable Task Scheduler: limitations](https://learn.microsoft.com/azure/durable-task/scheduler/durable-task-scheduler#limitations-and-considerations)
- [Large payload support (preview)](https://learn.microsoft.com/azure/durable-task/scheduler/durable-task-scheduler-large-payloads)
- [Python large payload sample](https://github.com/microsoft/durabletask-python/tree/main/examples/large_payload)
- [Keep inputs and outputs small](https://learn.microsoft.com/azure/durable-task/durable-functions/durable-functions-serialization-and-persistence#keep-durable-functions-inputs-and-outputs-small)
- [Durable Extension: state lifecycle and capacity](https://learn.microsoft.com/agent-framework/hosting/durable-extension)
