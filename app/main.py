"""
FastAPI surface for the Revenue Recovery agent.

Endpoint groups, added day by day:
  /health                     liveness
  /payments, /payments/...    raw batch views          (Day 1-2)
  /detect/...                 detect stage             (Day 2)
  /agent/...                  diagnose -> decide -> act (Day 3-4)
  /audit, /metrics            audit trail + batch report (Day 5-6)

One convention worth stating: every endpoint that could touch a customer defaults
to `dry_run=true`. Reaching out for real requires `?dry_run=false` in the URL, so
nobody triggers a live batch by clicking around in /docs.
"""

from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse

from app import agent as agent_stage
from app import detect as detect_stage
from app import metrics as metrics_stage
from app.audit import default_trail
from app.config import settings
from app.models import (
    ActionType,
    AgentRun,
    AuditEvent,
    AuditStage,
    AuditVerification,
    CaseRecord,
    DetectionBatch,
    FailedPayment,
    MetricsReport,
    RejectedRecord,
)
from app.services import llm_client, razorpay_client

app = FastAPI(
    title="Revenue Recovery Agent",
    description=(
        "Detects at-risk revenue (failed payments) and runs a bounded, auditable "
        "recovery workflow: detect -> diagnose -> decide -> act -> stop."
    ),
    version="0.6.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DATA_PATH: Path = detect_stage.DEFAULT_DATA_PATH

#: Last run held in memory so /metrics and /agent/decisions can answer without
#: re-running the batch (and, in live mode, without creating a second set of
#: payment links). The audit trail on disk is the durable record; this is a cache.
_last_run: Optional[AgentRun] = None


def _run_detect() -> DetectionBatch:
    """Run the detect stage, turning a missing batch file into a clean 404."""
    try:
        return detect_stage.detect(DATA_PATH)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _require_last_run() -> AgentRun:
    if _last_run is None:
        raise HTTPException(
            status_code=409,
            detail="No run in memory yet. POST /agent/run first (defaults to a dry run).",
        )
    return _last_run


@app.get("/health")
def health():
    """Liveness, plus an honest statement of which integrations are actually wired."""
    return {
        "status": "ok",
        "batch_file_present": DATA_PATH.exists(),
        "razorpay_configured": razorpay_client.is_configured(),
        "llm_configured": llm_client.is_available(),
        "llm_unavailable_reason": llm_client.unavailable_reason(),
        "policy": {
            "max_retry_attempts": settings.max_retry_attempts,
            "retry_cooldown_hours": settings.retry_cooldown_hours,
        },
        "last_run_id": _last_run.run_id if _last_run else None,
    }


# --------------------------------------------------------------------------
# Detect stage (Day 2)
# --------------------------------------------------------------------------

@app.get("/detect/run", response_model=DetectionBatch)
def detect_run():
    """
    Full detect stage output: every normalized at-risk case, every rejected row,
    and the aggregate summary. This is the input the agent's diagnose stage
    consumes in Day 3.
    """
    return _run_detect()


@app.get("/detect/summary")
def detect_summary():
    """
    Aggregate view of the batch. `unrecoverable_inr` is reported as a headline
    figure on purpose: money the agent deliberately declines to chase is a
    result, not an omission.
    """
    batch = _run_detect()
    return {
        "detected_at": batch.detected_at,
        "source": batch.source,
        "rows_read": batch.rows_read,
        "summary": batch.summary,
    }


@app.get("/detect/rejected", response_model=list[RejectedRecord])
def detect_rejected():
    """
    Rows that could not be ingested, with the reason for each. Surfaced as its
    own endpoint because "we couldn't read these" has to be visible, not buried
    in a log.
    """
    return _run_detect().rejected


@app.get("/detect/unrecoverable", response_model=list[FailedPayment])
def detect_unrecoverable():
    """
    Cases the agent is forbidden from acting on (fraud_suspected, card_blocked,
    customer_cancelled). Every one of these exits the workflow with
    STOP_NO_ACTION — see /agent/stopped.
    """
    return [c for c in _run_detect().cases if c.recoverability.value == "unrecoverable"]


# --------------------------------------------------------------------------
# Agent (Day 3-4)
# --------------------------------------------------------------------------

@app.post("/agent/run", response_model=AgentRun)
def agent_run(
    dry_run: bool = Query(
        True,
        description=(
            "TRUE (default) makes no external calls at all. Set FALSE to create real "
            "Razorpay test-mode payment links and orders."
        ),
    ),
    use_llm: bool = Query(True, description="Use the LLM for diagnosis. Falls back to rules if unavailable."),
    limit: Optional[int] = Query(None, ge=1, le=1000, description="Process only the first N cases."),
    settle: bool = Query(True, description="Apply the seeded outcome model to attempted cases."),
):
    """
    Run the whole workflow: detect -> diagnose -> decide -> act -> stop.

    Every step of every case is appended to the audit trail before the next one
    begins. Defaults to a dry run; `dry_run=false` is the only way to make this
    endpoint talk to Razorpay.
    """
    global _last_run
    try:
        run = agent_stage.run_batch(
            path=DATA_PATH, dry_run=dry_run, use_llm=use_llm, limit=limit, settle=settle
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    _last_run = run
    return run


@app.get("/agent/decisions", response_model=list[CaseRecord])
def agent_decisions(
    action: Optional[ActionType] = Query(None, description="Filter to one action type."),
):
    """Every case from the last run with its diagnosis, decision and outcome."""
    run = _require_last_run()
    if action is None:
        return run.records
    return [r for r in run.records if r.decision.action is action]


@app.get("/agent/stopped", response_model=list[CaseRecord])
def agent_stopped():
    """
    The cases the agent refused to act on, with the rule that stopped each one.

    This is the endpoint to read first. `/metrics` claims 100% stop compliance;
    this is the list that claim is computed from.
    """
    run = _require_last_run()
    return [r for r in run.records if r.decision.action is ActionType.STOP_NO_ACTION]


@app.get("/agent/escalations", response_model=list[CaseRecord])
def agent_escalations():
    """Cases handed to a human: unknown cause, no contact channel, retry cap spent."""
    run = _require_last_run()
    return [r for r in run.records if r.decision.action is ActionType.ESCALATE_TO_HUMAN]


@app.get("/agent/links")
def agent_links():
    """
    The Razorpay artefacts the last run created, with payable URLs.

    Pay one of these by hand with a test card, then call /agent/reconcile — that
    round trip is what turns a modeled rupee into a verified one.
    """
    run = _require_last_run()
    return [
        {
            "payment_id": r.case.payment_id,
            "amount_inr": r.case.amount_inr,
            "action": r.decision.action.value,
            "status": r.outcome.status.value,
            "provider_object": r.outcome.provider_object,
            "provider_ref": r.outcome.provider_ref,
            "short_url": r.outcome.provider_short_url,
            "verifiable": r.outcome.provider_object == "payment_link"
            and not r.outcome.provider_ref.startswith("dryrun_"),
        }
        for r in run.records
        if r.outcome.provider_ref
    ]


@app.post("/agent/reconcile")
def agent_reconcile(
    run_id: Optional[str] = Query(None, description="Defaults to the last run in memory."),
):
    """
    Ask Razorpay which of a run's payment links have actually been paid, and
    record the confirmed ones as VERIFIED_API.

    Separate from /agent/run because a human has to pay a test link in between.
    Link ids are read back out of the audit trail, not a side table.
    """
    target = run_id or (_last_run.run_id if _last_run else None)
    if not target:
        raise HTTPException(status_code=409, detail="No run_id given and no run in memory.")
    return agent_stage.reconcile_run(target)


# --------------------------------------------------------------------------
# Metrics (Day 6)
# --------------------------------------------------------------------------

@app.get("/metrics", response_model=MetricsReport)
def metrics():
    """
    The batch report for the last run.

    `recovered_verified` and `recovered_modeled` are separate fields and there is
    no field that adds them. `stop_compliance_pct` must read 100.0, and
    `reconciliation` carries the report's own arithmetic self-checks.
    """
    return _require_last_run().metrics


@app.get("/metrics/text", response_class=PlainTextResponse)
def metrics_text():
    """The same report rendered for a terminal."""
    return metrics_stage.render_text_report(_require_last_run().metrics)


# --------------------------------------------------------------------------
# Audit trail (Day 5)
# --------------------------------------------------------------------------

@app.get("/audit", response_model=list[AuditEvent])
def audit(
    run_id: Optional[str] = None,
    payment_id: Optional[str] = None,
    stage: Optional[AuditStage] = None,
    limit: int = Query(200, ge=1, le=5000),
):
    """The append-only trail, filterable. Every entry explains itself in `summary`."""
    return default_trail.query(run_id=run_id, payment_id=payment_id, stage=stage, limit=limit)


@app.get("/audit/verify", response_model=AuditVerification)
def audit_verify():
    """
    Re-walk the hash chain and report whether the record has been altered.

    An audit trail nobody can check is decoration. `ok=false` means the history is
    no longer trustworthy and names the sequence number where it breaks.
    """
    return default_trail.verify()


@app.get("/audit/runs")
def audit_runs():
    """Run ids present in the trail, oldest first."""
    return {"runs": default_trail.run_ids()}


# --------------------------------------------------------------------------
# Raw batch views (Day 1-2)
# --------------------------------------------------------------------------

@app.get("/payments", response_model=list[FailedPayment])
def list_failed_payments(limit: int = Query(100, ge=1, le=1000)):
    """The normalized at-risk batch (valid cases only)."""
    return _run_detect().cases[:limit]


@app.get("/payments/summary")
def payments_summary():
    """Counts and ₹ at risk, grouped by failure reason."""
    batch = _run_detect()
    return {
        "total_records": batch.summary.total_cases,
        "total_amount_at_risk_inr": batch.summary.total_at_risk_inr,
        "by_reason": {
            reason: {"count": b.count, "total_amount_inr": b.amount_inr}
            for reason, b in sorted(batch.summary.by_reason.items())
        },
    }


@app.get("/payments/{payment_id}", response_model=FailedPayment)
def get_payment(payment_id: str):
    for case in _run_detect().cases:
        if case.payment_id == payment_id:
            return case
    raise HTTPException(status_code=404, detail=f"No case with payment_id {payment_id}")
