"""
FastAPI surface for the Revenue Recovery agent.

Endpoint groups, added day by day:
  /health                     liveness
  /payments, /payments/...    raw batch views          (Day 1-2)
  /detect/...                 detect stage             (Day 2)
  /agent/...                  diagnose -> decide -> act (Day 3-4)
  /audit, /metrics            audit trail + batch report (Day 5-6)
"""

from pathlib import Path

from fastapi import FastAPI, HTTPException, Query

from app import detect as detect_stage
from app.models import DetectionBatch, FailedPayment, RejectedRecord

app = FastAPI(
    title="Revenue Recovery Agent",
    description=(
        "Detects at-risk revenue (failed payments) and runs a bounded, auditable "
        "recovery workflow: detect -> diagnose -> decide -> act -> stop."
    ),
    version="0.2.0",
)

DATA_PATH: Path = detect_stage.DEFAULT_DATA_PATH


def _run_detect() -> DetectionBatch:
    """Run the detect stage, turning a missing batch file into a clean 404."""
    try:
        return detect_stage.detect(DATA_PATH)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/health")
def health():
    return {"status": "ok", "batch_file_present": DATA_PATH.exists()}


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
    customer_cancelled). Day 3 proves every one of these exits the workflow with
    STOP_NO_ACTION.
    """
    return [c for c in _run_detect().cases if c.recoverability.value == "unrecoverable"]


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
