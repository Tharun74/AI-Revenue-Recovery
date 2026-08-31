"""
Batch report.

One rule governs this module: **verified rupees and modeled rupees are never
summed.** `recovered_verified` and `recovered_modeled` are separate lines with
separate rates, and no function here produces a combined "₹ recovered" figure. A
single blended number would be the easiest claim in this project to disbelieve,
and the honest version costs nothing but a second column.

Three other choices worth defending:

* **The denominator for a recovery rate is ₹ attempted, not ₹ at risk.** Dividing
  by the whole batch would let the agent improve its own score by declining to
  chase things, which is precisely backwards.

* **Restraint gets headline lines, not a footnote.**
  `deliberately_not_chased` (the hard gate) and `withheld_this_run` (cooldown and
  retry-cap holds) sit alongside the recovery figures. `stop_compliance_pct` must
  read 100.0; anything less is a failed run rather than a low score.

* **Classification here is post-diagnosis.** The buckets use each decision's
  *effective* recoverability, so a case the diagnose stage narrowed from
  recoverable to unrecoverable is reported where it actually ended up, not where
  the CSV's reason string first put it.

Exceptions are itemised, never aggregated away: unreadable input rows and failed
API calls both appear in `unresolved_exceptions` with a reference and a reason.
"""

from __future__ import annotations

from app.models import (
    ActionStatus,
    ActionType,
    CaseRecord,
    DiagnosisSource,
    ExceptionItem,
    MetricsReport,
    PolicyRule,
    Recoverability,
    RejectedRecord,
    SettlementSource,
)

#: Rules that hold recoverable money back on purpose this run. Distinct from the
#: hard gate: this money stays recoverable and a later run may well collect it.
_WITHHOLDING_RULES = frozenset({
    PolicyRule.STOP_COOLDOWN_NOT_ELAPSED,
    PolicyRule.ESCALATE_RETRY_CAP_REACHED,
})

#: Statuses that mean an action was actually carried out (really or simulated).
_CARRIED_OUT = frozenset({ActionStatus.EXECUTED, ActionStatus.SIMULATED})


def _bump(counter: dict[str, int], key: str) -> None:
    counter[key] = counter.get(key, 0) + 1


def build_report(
    run_id: str,
    records: list[CaseRecord],
    rejected: list[RejectedRecord],
    rows_read: int,
    source: str = "",
    dry_run: bool = True,
    llm_used: bool = False,
    partial_run: bool = False,
) -> MetricsReport:
    """Aggregate one run's case records into the report."""
    report = MetricsReport(
        run_id=run_id, source=source, dry_run=dry_run, llm_used=llm_used, rows_read=rows_read,
        partial_run=partial_run,
    )

    for record in records:
        case, diagnosis, decision, outcome = (
            record.case, record.diagnosis, record.decision, record.outcome
        )
        amount = case.amount_paise

        report.at_risk.add(amount)

        if decision.recoverability is Recoverability.UNRECOVERABLE:
            report.unrecoverable.add(amount)
        elif decision.recoverability is Recoverability.UNKNOWN:
            report.unknown_cause.add(amount)
        else:
            report.recoverable.add(amount)

        _bump(report.by_action, decision.action.value)
        _bump(report.by_policy_rule, decision.policy_rule.value)
        _bump(report.by_diagnosis_source, diagnosis.source.value)
        report.boundary_violations_refused += len(diagnosis.boundary_violations)

        # The hard gate, counted as a result.
        if decision.policy_rule is PolicyRule.GATE_UNRECOVERABLE_CAUSE:
            report.deliberately_not_chased.add(amount)
        if decision.policy_rule in _WITHHOLDING_RULES:
            report.withheld_this_run.add(amount)

        if decision.recoverability is Recoverability.UNRECOVERABLE:
            report.gated_cases += 1
            stopped_cleanly = (
                decision.action is ActionType.STOP_NO_ACTION
                and outcome.status is ActionStatus.NO_ACTION
                and not outcome.provider_ref
                and outcome.recovered_paise == 0
            )
            if stopped_cleanly:
                report.correctly_stopped_cases += 1

        if decision.action is ActionType.ESCALATE_TO_HUMAN:
            report.escalations.add(amount)
            _bump(report.escalations_by_rule, decision.policy_rule.value)

        # Attempted: a contact action was carried out. This is the denominator of
        # both recovery rates.
        if decision.contacts_customer and outcome.status in _CARRIED_OUT:
            report.attempted.add(amount)

        # Contacts that genuinely left the process. Zero in a dry run, and saying
        # so is the point of having a separate field from `attempted`.
        if decision.contacts_customer and outcome.status is ActionStatus.EXECUTED:
            report.customer_contacts_made += 1

        # The two settlement columns. Mutually exclusive by construction, and
        # asserted so below.
        if outcome.settlement_source is SettlementSource.VERIFIED_API:
            report.recovered_verified.add(outcome.recovered_paise)
        elif outcome.settlement_source is SettlementSource.MODELED:
            report.recovered_modeled.add(outcome.recovered_paise)

        if outcome.status is ActionStatus.FAILED:
            report.unresolved_exceptions.append(
                ExceptionItem(
                    kind="action_failed",
                    reference=case.payment_id,
                    detail=f"{decision.action.value}: {outcome.error or outcome.detail}",
                    amount_paise=amount,
                )
            )

    # Rows that never became cases are exceptions too — money that vanished from
    # the batch before the agent could reason about it.
    for rec in rejected:
        report.unresolved_exceptions.append(
            ExceptionItem(
                kind="rejected_row",
                reference=rec.payment_id or f"row {rec.row_number}",
                detail=f"{rec.reject_reason.value}: {rec.detail}",
            )
        )

    report.reconciliation = _check_invariants(report, records, rejected, rows_read)
    return report


def _check_invariants(
    report: MetricsReport,
    records: list[CaseRecord],
    rejected: list[RejectedRecord],
    rows_read: int,
) -> dict[str, bool]:
    """
    Self-checks shipped with the report.

    A report that cannot prove its own arithmetic is a claim, not a measurement,
    so the checks travel with the numbers instead of living only in the test
    suite. `all_invariants_hold` on the report is the single thing a reviewer has
    to look at.
    """
    gated = [r for r in records if r.decision.recoverability is Recoverability.UNRECOVERABLE]
    processed = len(records) + len(rejected)

    return {
        # Nothing vanished between the CSV and the report. A deliberately
        # truncated run can only claim the weaker inequality, and says so via
        # `partial_run` rather than quietly failing the check.
        "every_input_row_accounted_for": (
            processed <= rows_read if report.partial_run else processed == rows_read
        ),
        # Every case got exactly one action.
        "every_case_has_exactly_one_action": sum(report.by_action.values()) == len(records),
        "every_case_has_exactly_one_policy_rule": (
            sum(report.by_policy_rule.values()) == len(records)
        ),
        # Money buckets partition the batch.
        "money_buckets_partition_the_batch": (
            report.recoverable.amount_paise
            + report.unrecoverable.amount_paise
            + report.unknown_cause.amount_paise
            == report.at_risk.amount_paise
        ),
        "case_buckets_partition_the_batch": (
            report.recoverable.cases + report.unrecoverable.cases + report.unknown_cause.cases
            == report.at_risk.cases
        ),
        # The gate held. Both halves matter: every gated case stopped, and none of
        # them produced a Razorpay artefact of any kind.
        "every_gated_case_stopped": report.correctly_stopped_cases == report.gated_cases,
        "no_gated_case_was_contacted": all(
            not r.outcome.provider_ref and r.outcome.recovered_paise == 0 for r in gated
        ),
        "gate_count_matches_gate_rule": (
            report.by_policy_rule.get(PolicyRule.GATE_UNRECOVERABLE_CAUSE.value, 0)
            == report.gated_cases
        ),
        # Recovery cannot exceed what was attempted. Checked per column, never on
        # a sum of the two.
        "verified_within_attempted": (
            report.recovered_verified.amount_paise <= report.attempted.amount_paise
        ),
        "modeled_within_attempted": (
            report.recovered_modeled.amount_paise <= report.attempted.amount_paise
        ),
        "settlement_columns_are_disjoint": (
            report.recovered_verified.cases + report.recovered_modeled.cases
            <= report.attempted.cases
        ),
        # Nothing was recovered from a case the agent never acted on.
        "no_recovery_without_an_action": all(
            r.outcome.recovered_paise == 0
            for r in records
            if not (r.decision.contacts_customer and r.outcome.status in _CARRIED_OUT)
        ),
        # A dry run must not have touched anybody.
        "dry_run_made_no_contact": (
            report.customer_contacts_made == 0 if report.dry_run else True
        ),
    }


def render_text_report(report: MetricsReport) -> str:
    """
    Plain-text rendering for the terminal and the pitch video. Same numbers as the
    JSON, same separation of the two settlement columns.
    """
    r = report

    def line(label: str, ml, note: str = "") -> str:
        body = f"  {label:<34} {ml.cases:>4} cases   INR {ml.amount_inr:>13,.2f}"
        return f"{body}   {note}" if note else body

    mode = "DRY RUN (no external calls)" if r.dry_run else "LIVE (Razorpay test mode)"
    out = [
        "=" * 78,
        f"REVENUE RECOVERY — BATCH REPORT   run {r.run_id}",
        "=" * 78,
        f"  mode: {mode}",
        f"  diagnosis: {'LLM + deterministic gate' if r.llm_used else 'deterministic (LLM not used)'}",
        f"  rows read: {r.rows_read}   generated: {r.generated_at.isoformat(timespec='seconds')}",
    ]
    if r.partial_run:
        out.append(
            f"  PARTIAL RUN: only {r.at_risk.cases} cases processed, so batch totals below "
            "cover part of the file."
        )
    out += [
        "",
        "DETECTED",
        line("at risk", r.at_risk),
        line("recoverable", r.recoverable),
        line("unrecoverable (gated)", r.unrecoverable),
        line("unknown cause", r.unknown_cause),
        "",
        "ACTED",
        line("attempted", r.attempted),
        f"  {'real customer contacts made':<34} {r.customer_contacts_made:>4}",
        "",
        "RECOVERED — the two columns are never added together",
        line("verified (Razorpay confirmed)", r.recovered_verified,
             f"{r.recovery_rate_verified_pct:.2f}% of attempted"),
        line("modeled (seeded model)", r.recovered_modeled,
             f"{r.recovery_rate_modeled_pct:.2f}% of attempted"),
        "",
        "RESTRAINT",
        line("deliberately not chased", r.deliberately_not_chased),
        line("withheld this run", r.withheld_this_run, "(cooldown / retry cap)"),
        line("escalated to a human", r.escalations),
        f"  {'gated causes correctly stopped':<34} "
        f"{r.correctly_stopped_cases:>4} / {r.gated_cases}   "
        f"{r.stop_compliance_pct:.2f}%",
        f"  {'LLM overreach refused':<34} {r.boundary_violations_refused:>4}",
        "",
        "ACTIONS",
    ]
    for action, count in sorted(r.by_action.items(), key=lambda kv: -kv[1]):
        out.append(f"  {action:<34} {count:>4}")
    out.append("")
    out.append("POLICY RULE THAT FIRED")
    for rule, count in sorted(r.by_policy_rule.items(), key=lambda kv: -kv[1]):
        out.append(f"  {rule:<34} {count:>4}")

    out.append("")
    out.append(f"UNRESOLVED EXCEPTIONS ({r.unresolved_exception_count})")
    for item in r.unresolved_exceptions[:20]:
        out.append(f"  [{item.kind}] {item.reference}: {item.detail[:88]}")
    if r.unresolved_exception_count > 20:
        out.append(f"  ... and {r.unresolved_exception_count - 20} more")

    out.append("")
    out.append("INVARIANTS")
    for name, ok in r.reconciliation.items():
        out.append(f"  {'PASS' if ok else 'FAIL'}  {name}")
    out.append("")
    out.append(f"  ALL INVARIANTS HOLD: {r.all_invariants_hold}")
    out.append("=" * 78)
    return "\n".join(out)


__all__ = ["build_report", "render_text_report"]
