"""pipeline_status — reads Step Functions executions and Glue job runs, summarises failures.

The observability agent. Answers "did last night's pipeline run?" with the actual state
of the system rather than a guess, and when something failed it says which stage and why.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import boto3

from agents.config import get_config

logger = logging.getLogger(__name__)

PIPELINE_STATUS_SPEC = {
    "toolSpec": {
        "name": "pipeline_status",
        "description": (
            "Report the health of the data pipeline: recent Step Functions executions, "
            "recent Glue job runs, and the cause of any failure. Use for questions about "
            "whether the pipeline ran, whether it succeeded, when it last completed, or "
            "why it failed — as opposed to questions about the transaction data itself."
        ),
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {
                    "max_executions": {
                        "type": "integer",
                        "description": "How many recent executions to inspect (default 5).",
                    }
                },
            }
        },
    }
}


def pipeline_status(max_executions: int = 5) -> dict[str, Any]:
    config = get_config()

    if not config.state_machine_arn:
        return {"status": "unavailable", "error": "STATE_MACHINE_ARN is unset", "executions": []}

    sfn = boto3.client("stepfunctions", region_name=config.region)
    glue = boto3.client("glue", region_name=config.region)

    executions = _recent_executions(sfn, config.state_machine_arn, max_executions)
    jobs = _recent_glue_runs(glue, config.glue_job_prefix)

    succeeded = [e for e in executions if e["status"] == "SUCCEEDED"]
    failed = [e for e in executions if e["status"] in ("FAILED", "TIMED_OUT", "ABORTED")]

    last_success = succeeded[0]["stopped_at"] if succeeded else None
    hours_since = None
    if last_success:
        hours_since = round((datetime.now(UTC) - last_success).total_seconds() / 3600, 2)

    return {
        "status": "ok",
        "executions": [_serialise_execution(e) for e in executions],
        "glue_job_runs": jobs,
        "summary": {
            "inspected": len(executions),
            "succeeded": len(succeeded),
            "failed": len(failed),
            "last_success_at": last_success.isoformat() if last_success else None,
            "hours_since_last_success": hours_since,
            # The freshness alarm uses 3 hours. Reporting the same judgement here means
            # the agent and the alarm cannot disagree about whether the pipeline is stale.
            "is_stale": hours_since is None or hours_since > 3,
        },
        "failure_details": [_failure_detail(sfn, e) for e in failed],
    }


def _recent_executions(sfn, state_machine_arn: str, limit: int) -> list[dict[str, Any]]:
    response = sfn.list_executions(stateMachineArn=state_machine_arn, maxResults=min(limit, 100))
    return [
        {
            "name": item["name"],
            "arn": item["executionArn"],
            "status": item["status"],
            "started_at": item["startDate"],
            "stopped_at": item.get("stopDate"),
        }
        for item in response.get("executions", [])
    ]


def _serialise_execution(execution: dict[str, Any]) -> dict[str, Any]:
    started, stopped = execution["started_at"], execution.get("stopped_at")
    return {
        "name": execution["name"],
        "status": execution["status"],
        "started_at": started.isoformat() if started else None,
        "stopped_at": stopped.isoformat() if stopped else None,
        "duration_seconds": round((stopped - started).total_seconds(), 1) if started and stopped else None,
    }


def _failure_detail(sfn, execution: dict[str, Any]) -> dict[str, Any]:
    """Walk the execution history backwards to the state that actually failed.

    `describe_execution` reports only that the execution failed. The useful answer is
    *which state* failed and with what cause, and that only exists in the event history.
    """
    detail: dict[str, Any] = {"execution": execution["name"], "status": execution["status"]}

    try:
        history = sfn.get_execution_history(
            executionArn=execution["arn"],
            reverseOrder=True,
            maxResults=50,
            includeExecutionData=True,
        )
    except Exception as exc:  # noqa: BLE001
        detail["error"] = f"could not read execution history: {exc}"
        return detail

    for event in history.get("events", []):
        for key in (
            "executionFailedEventDetails",
            "taskFailedEventDetails",
            "lambdaFunctionFailedEventDetails",
        ):
            failure = event.get(key)
            if failure:
                detail["failed_at"] = event.get("timestamp").isoformat() if event.get("timestamp") else None
                detail["error"] = failure.get("error")
                detail["cause"] = (failure.get("cause") or "")[:1000]
                return detail

    detail["error"] = "no failure event found in the inspected history window"
    return detail


def _recent_glue_runs(glue, job_prefix: str, per_job: int = 3) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []

    try:
        job_names = [
            name for name in glue.list_jobs(MaxResults=100).get("JobNames", []) if name.startswith(job_prefix)
        ]
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not list Glue jobs: %s", exc)
        return runs

    for name in job_names:
        try:
            response = glue.get_job_runs(JobName=name, MaxResults=per_job)
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not read runs for %s: %s", name, exc)
            continue

        for run in response.get("JobRuns", []):
            runs.append(
                {
                    "job_name": name,
                    "run_id": run["Id"],
                    "state": run["JobRunState"],
                    "started_at": run["StartedOn"].isoformat() if run.get("StartedOn") else None,
                    "execution_time_seconds": run.get("ExecutionTime"),
                    # Truncated: a Spark stack trace is thousands of tokens and the first
                    # line is what identifies the failure.
                    "error_message": (run.get("ErrorMessage") or "")[:500] or None,
                    "dpu_seconds": run.get("DPUSeconds"),
                }
            )

    runs.sort(key=lambda r: r["started_at"] or "", reverse=True)
    return runs
