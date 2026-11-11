#!/usr/bin/env bash
# End-to-end crash/recovery demo.
#  1. ensures agentgateway (port 4000) and the DTS emulator (8080/8082) are running
#  2. starts a worker that hard-crashes while saving section $CRASH_ON (default 3)
#  3. starts a long report run
#  4. after the crash, starts a fresh worker -> the run resumes, finished LLM calls are not repeated
#
# Usage:  [MODE=orchestration|workflow|agent|durable-tools] [SECTIONS=6] [CRASH_ON=3] ./demo.sh ["topic"]
#   MODE=orchestration (default)  worker.py          Durable Task orchestrator in plain Python
#   MODE=workflow                 workflow_worker.py MAF workflow graph hosted by the durable extension
#   MODE=agent                    agent_worker.py          one agent with tools, its own tool loop (one checkpoint)
#   MODE=durable-tools            durable_tools_worker.py  same agent, every model call and tool call checkpointed
set -euo pipefail
cd "$(dirname "$0")"

MODE="${MODE:-orchestration}"
case "$MODE" in
  orchestration) WORKER=worker.py ;;
  workflow) WORKER=workflow_worker.py ;;
  agent) WORKER=agent_worker.py ;;
  durable-tools) WORKER=durable_tools_worker.py ;;
  *) echo "MODE must be orchestration, workflow, agent or durable-tools (got '$MODE')" >&2; exit 2 ;;
esac

CRASH_ON="${CRASH_ON:-3}"
SECTIONS="${SECTIONS:-6}"
TOPIC="${1:-Durable execution for AI agents: why checkpointing matters in production}"
LOG_DIR=output/logs
mkdir -p "$LOG_DIR"
: > output/llm_calls.log   # fresh call log per demo run

# --- infrastructure ----------------------------------------------------------
if ! ss -ltn | grep -q ':4000 '; then
  echo "▶ starting agentgateway"
  mkdir -p ~/.local/state/agentgateway
  nohup agentgateway -f ~/.config/agentgateway/config.yaml >~/.local/state/agentgateway/agentgateway.log 2>&1 &
  sleep 2
fi
if ! docker ps --format '{{.Names}}' | grep -qx dts-emulator; then
  echo "▶ starting Durable Task Scheduler emulator"
  docker start dts-emulator >/dev/null 2>&1 ||
    docker run -d --name dts-emulator -e DTS_TASK_HUB_NAMES=durable-maf -p 8080:8080 -p 8082:8082 mcr.microsoft.com/dts/dts-emulator:latest >/dev/null
  sleep 3
fi

# --- run 1: worker that will crash -------------------------------------------
echo "▶ mode: $MODE ($WORKER)"
echo "▶ worker #1 (will crash on section $CRASH_ON)"
CRASH_ON_SECTION="$CRASH_ON" uv run "$WORKER" >"$LOG_DIR/worker1.log" 2>&1 &
W1=$!
sleep 4
kill -0 "$W1" 2>/dev/null || { echo "worker #1 failed to start:"; cat "$LOG_DIR/worker1.log"; exit 1; }

ID=$(uv run client.py start "$TOPIC" --sections "$SECTIONS" --mode "$MODE")
echo "▶ run $ID started — dashboard: http://localhost:8082 (task hub: durable-maf)"

# follow progress in the background
uv run client.py wait "$ID" &
WAITER=$!

set +e
wait "$W1"
echo "💥 worker #1 exited with code $? — orchestration is still in the scheduler, nothing is running"
set -e
sleep 5

# --- run 2: fresh worker picks up where #1 died ------------------------------
echo "▶ worker #2 (no crash) — resuming"
uv run "$WORKER" >"$LOG_DIR/worker2.log" 2>&1 &
W2=$!
sleep 4
kill -0 "$W2" 2>/dev/null || { echo "worker #2 failed to start:"; cat "$LOG_DIR/worker2.log"; kill "$WAITER"; exit 1; }

set +e
wait "$WAITER"
RC=$?
set -e
kill "$W2" 2>/dev/null || true

echo
echo "── llm_calls.log: real model calls (and tool calls in agent mode), with worker pid ──"
cat output/llm_calls.log
echo
echo "report: output/$ID/report.md"
exit $RC
