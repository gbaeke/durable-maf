"""Shared configuration: Durable Task Scheduler emulator + agentgateway settings."""

import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from durabletask.azuremanaged.client import DurableTaskSchedulerClient
from durabletask.azuremanaged.worker import DurableTaskSchedulerWorker

load_dotenv(Path(__file__).parent / ".env")

# Local Durable Task Scheduler emulator (docker: mcr.microsoft.com/dts/dts-emulator)
DTS_ENDPOINT = os.getenv("DTS_ENDPOINT", "http://localhost:8080")
TASKHUB = os.getenv("TASKHUB", "durable-maf")

# agentgateway (OpenAI-compatible) on this machine
GATEWAY_URL = os.getenv("GATEWAY_URL", "http://localhost:4000/v1")
MODEL = os.getenv("MODEL", "gpt-6-luna")

OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", Path(__file__).parent / "output"))
ORCHESTRATION_NAME = "research_report"  # worker.py
WORKFLOW_NAME = "report_workflow"  # workflow_worker.py (orchestrator: dafx-report_workflow)
AGENT_LOOP_NAME = "agent_loop"  # agent_worker.py
DURABLE_TOOLS_NAME = "tool_loop"  # durable_tools_worker.py


def _dts_kwargs(log_handler: logging.Handler | None) -> dict:
    # The emulator needs no credential and no TLS.
    return dict(  # noqa: C408
        host_address=DTS_ENDPOINT,
        secure_channel=False,
        taskhub=TASKHUB,
        token_credential=None,
        log_handler=log_handler,
    )


def get_worker(log_handler: logging.Handler | None = None) -> DurableTaskSchedulerWorker:
    return DurableTaskSchedulerWorker(**_dts_kwargs(log_handler))


def get_client(log_handler: logging.Handler | None = None) -> DurableTaskSchedulerClient:
    return DurableTaskSchedulerClient(**_dts_kwargs(log_handler))
