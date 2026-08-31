"""
Orchestrator: detect -> diagnose -> decide -> act -> stop, with every step written
to the audit trail as it happens.

The loop is deliberately flat and synchronous. You can read `run_batch` top to
bottom and see every point at which the agent could touch a customer, which is
the property that matters most in a system whose main safety claim is "it stops".

Ordering that is load-bearing:

1. **Audit before act.** The decision is appended to the trail *before* the action
   is executed. If the process dies mid-call there is a record of what it was
   about to do, rather than a Razorpay object nobody can account for.
2. **Reconcile before model.** `reconcile_run` upgrades outcomes to
   `VERIFIED_API` first; the seeded model only fills in what Razorpay could not
   confirm. A model never overwrites a verified figure.
3. **Diagnose behind the gate.** Enforced inside app/diagnose.py, but the loop
   also never reaches an action for a gated case, because app/decide.py checks the
   gate as rule 1 of 8.

`dry_run` defaults to True everywhere. Nothing in this project reaches out to a
real customer unless a caller asked for it in so many words.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from app import act as act_stage
from app import decide as decide_stage
from app import detect as detect_stage
from app import diagnose as diagnose_stage
from app import metrics as metrics_stage
from app.audit import AuditTrail, default_trail
from app.models import (
    ActionStatus,
    AgentRun,
    AuditStage,
    CaseRecord,
    DiagnosisSource,
    SettlementSource,
    utcnow,
)


def new_run_id() -> str:
    """Short, sortable-ish run id. Appears on every audit entry and every
    Razorpay note, so an artefact in the dashboard can be traced to a run."""
    return f"run_{utcnow().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}"


def run_batch(
    path: Optional[Path] = None,
    rows: Optional[list[dict]] = None,
    dry_run: bool = True,
    use_llm: bool = True,
    limit: Optional[int] = None,
    trail: Optional[AuditTrail] = None,
    now: Optional[datetime] = None,
    settle: bool = True,
) -> AgentRun:
    """
    Run the full workflow over one batch.

    `settle=False` skips the seeded outcome model, which is how a caller inspects
    the agent's *decisions* without any modeled money in the picture at all.
    """
    trail = trail or default_trail
    run_id = new_run_id()
    started_at = now or utcnow()

    batch = detect_stage.detect(path=path, rows=rows, now=started_at)
    cases = batch.cases[:limit] if limit is not None else batch.cases

    trail.append(
        run_id=run_id,
        stage=AuditStage.RUN_STARTED,
        summary=(
            f"Run started over {batch.source}: {batch.rows_read} rows read, "
            f"{len(cases)} cases to process, {len(batch.rejected)} rows rejected. "
            f"mode={'dry_run' if dry_run else 'live'}"
        ),
        payload={
            "source": batch.source,
            "rows_read": batch.rows_read,
            "cases": len(cases),
            "rejected": len(batch.rejected),
            "dry_run": dry_run,
            "use_llm": use_llm,
            "detection_summary": batch.summary,
        },
        at=started_at,
    )

    # Unreadable rows are logged individually. A row silently dropped is money
    # silently vanishing, so each one gets its own audit entry.
    for rec in batch.rejected:
        trail.append(
            run_id=run_id,
            stage=AuditStage.ROW_REJECTED,
            payment_id=rec.payment_id,
            summary=f"Row {rec.row_number} rejected ({rec.reject_reason.value}): {rec.detail}",
            payload=rec,
        )

    session = diagnose_stage.DiagnoseSession(use_llm=use_llm)
    records: list[CaseRecord] = []

    for case in cases:
        trail.append(
            run_id=run_id,
            stage=AuditStage.CASE_DETECTED,
            payment_id=case.payment_id,
            summary=(
                f"At-risk case: INR {case.amount_inr:,.2f}, cause "
                f"'{case.failure_reason.value}', {case.recoverability.value}, "
                f"{case.retry_count} prior attempt(s)."
            ),
            payload=case,
        )

        diagnosis = diagnose_stage.diagnose(case, use_llm=use_llm, session=session)
        trail.append(
            run_id=run_id,
            stage=AuditStage.DIAGNOSED,
            payment_id=case.payment_id,
            summary=f"[{diagnosis.source.value}] {diagnosis.root_cause}",
            payload=diagnosis,
        )

        decision = decide_stage.decide(case, diagnosis, now=started_at)
        # Written before the action runs, on purpose — see the module docstring.
        trail.append(
            run_id=run_id,
            stage=AuditStage.DECIDED,
            payment_id=case.payment_id,
            summary=f"{decision.action.value} [{decision.policy_rule.value}] — {decision.reasoning}",
            payload=decision,
        )

        outcome = act_stage.execute(case, decision, run_id=run_id, dry_run=dry_run)
        trail.append(
            run_id=run_id,
            stage=AuditStage.ACTED,
            payment_id=case.payment_id,
            summary=(
                f"{outcome.status.value}: {outcome.detail}"
                + (f" ref={outcome.provider_ref}" if outcome.provider_ref else "")
                + (f" error={outcome.error}" if outcome.error else "")
            ),
            payload=outcome,
        )

        if settle:
            settled = act_stage.apply_modeled_settlement(case, decision, outcome)
            if settled.settlement_source is not outcome.settlement_source or settled.settlement_detail:
                trail.append(
                    run_id=run_id,
                    stage=AuditStage.SETTLED,
                    payment_id=case.payment_id,
                    summary=(
                        f"{settled.settlement_source.value}: INR {settled.recovered_inr:,.2f}. "
                        f"{settled.settlement_detail}"
                    ),
                    payload={
                        "settlement_source": settled.settlement_source.value,
                        "recovered_paise": settled.recovered_paise,
                        "detail": settled.settlement_detail,
                    },
                )
            outcome = settled

        records.append(
            CaseRecord(case=case, diagnosis=diagnosis, decision=decision, outcome=outcome)
        )

    llm_used = any(
        r.diagnosis.source in {DiagnosisSource.LLM, DiagnosisSource.LLM_CACHE} for r in records
    )
    report = metrics_stage.build_report(
        run_id=run_id,
        records=records,
        rejected=batch.rejected,
        rows_read=batch.rows_read,
        source=batch.source,
        dry_run=dry_run,
        llm_used=llm_used,
        partial_run=limit is not None and len(cases) < len(batch.cases),
    )

    finished_at = utcnow()
    trail.append(
        run_id=run_id,
        stage=AuditStage.RUN_COMPLETED,
        summary=(
            f"Run complete: {report.at_risk.cases} cases, "
            f"INR {report.at_risk.amount_inr:,.2f} at risk. "
            f"verified INR {report.recovered_verified.amount_inr:,.2f} / "
            f"modeled INR {report.recovered_modeled.amount_inr:,.2f} / "
            f"not chased INR {report.deliberately_not_chased.amount_inr:,.2f}. "
            f"stop compliance {report.stop_compliance_pct:.2f}%. "
            f"invariants_hold={report.all_invariants_hold}"
        ),
        payload=report,
        at=finished_at,
    )

    return AgentRun(
        run_id=run_id,
        started_at=started_at,
        finished_at=finished_at,
        dry_run=dry_run,
        source=batch.source,
        detection=batch.summary,
        records=records,
        metrics=report,
    )


def reconcile_run(
    run_id: str,
    trail: Optional[AuditTrail] = None,
) -> dict:
    """
    Re-check every payment link a past run created and report which ones Razorpay
    now confirms as paid.

    This is the verified-rupee path, and it is a separate call on purpose: the
    link has to be created, then paid by a human with a test card, then confirmed.
    That sequence cannot happen inside a single request.

    Reads link ids out of the audit trail rather than a side table — if the trail
    cannot answer "what did this run actually create", it is not much of a trail.
    """
    trail = trail or default_trail
    events = trail.query(run_id=run_id, stage=AuditStage.ACTED)

    checked: list[dict] = []
    verified_paise = 0
    verified_cases = 0

    for event in events:
        payload = event.payload or {}
        if payload.get("provider_object") != "payment_link":
            continue
        ref = str(payload.get("provider_ref") or "")
        if not ref or ref.startswith("dryrun_"):
            continue
        if payload.get("status") not in {ActionStatus.EXECUTED.value, ActionStatus.EXECUTED}:
            continue

        from app.models import ActionOutcome  # local import keeps the module graph flat

        try:
            outcome = ActionOutcome(**payload)
        except Exception as exc:
            checked.append({"payment_id": event.payment_id, "provider_ref": ref,
                            "error": f"unreadable audit payload: {exc}"})
            continue

        reconciled = act_stage.reconcile(outcome)
        is_verified = reconciled.settlement_source is SettlementSource.VERIFIED_API
        if is_verified:
            verified_cases += 1
            verified_paise += reconciled.recovered_paise
            trail.append(
                run_id=run_id,
                stage=AuditStage.SETTLED,
                payment_id=reconciled.payment_id,
                summary=f"verified_api: INR {reconciled.recovered_inr:,.2f}. {reconciled.settlement_detail}",
                payload={
                    "settlement_source": reconciled.settlement_source.value,
                    "recovered_paise": reconciled.recovered_paise,
                    "provider_ref": reconciled.provider_ref,
                    "detail": reconciled.settlement_detail,
                },
            )
        checked.append({
            "payment_id": reconciled.payment_id,
            "provider_ref": reconciled.provider_ref,
            "short_url": reconciled.provider_short_url,
            "verified": is_verified,
            "recovered_inr": reconciled.recovered_inr if is_verified else 0.0,
            "detail": reconciled.settlement_detail,
        })

    return {
        "run_id": run_id,
        "links_checked": len(checked),
        "verified_cases": verified_cases,
        "verified_paise": verified_paise,
        "verified_inr": round(verified_paise / 100, 2),
        "note": (
            "Verified figures come only from a Razorpay payment-link fetch reporting "
            "status=paid. Retry actions create Orders, which nobody can pay by hand, so "
            "they never appear here."
        ),
        "links": checked,
    }


__all__ = ["new_run_id", "reconcile_run", "run_batch"]
