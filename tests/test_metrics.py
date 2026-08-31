"""
Tests for the metrics report.

The headline test is `test_no_field_anywhere_sums_verified_and_modeled`, which
walks the whole serialised report looking for a number that equals
verified + modeled. It exists because the project's central honesty claim is
structural, and a structural claim should be enforced by a test rather than by
everyone remembering.

The rest checks that restraint is counted as a result, that the recovery-rate
denominator cannot be gamed, and that the report's self-checks actually fail when
something is wrong.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.detect import normalize_row
from app.metrics import build_report, render_text_report
from app.models import (
    ActionOutcome,
    ActionStatus,
    ActionType,
    CaseRecord,
    Decision,
    Diagnosis,
    DiagnosisSource,
    FailureReason,
    PolicyRule,
    Recoverability,
    RejectReason,
    RejectedRecord,
    SettlementSource,
)

NOW = datetime(2026, 8, 25, 12, 0, 0, tzinfo=timezone.utc)
RUN = "run_test_metrics"


def make_case(payment_id="pay_1", amount_inr="1000.00", reason="insufficient_funds", **overrides):
    row = {
        "payment_id": payment_id,
        "order_id": "order_1",
        "customer_id": "cust_1",
        "customer_name": "Aarav Sharma",
        "customer_email": "aarav@example.com",
        "customer_phone": "9876543210",
        "amount_inr": amount_inr,
        "currency": "INR",
        "failure_reason": reason,
        "error_description": "desc",
        "created_at": (NOW - timedelta(hours=30)).isoformat(),
        "retry_count": "0",
        "last_attempt_at": "",
    }
    row.update(overrides)
    case, rejection = normalize_row(1, row, set(), now=NOW)
    assert rejection is None, rejection
    return case


def record(
    payment_id="pay_1",
    amount_inr="1000.00",
    reason="insufficient_funds",
    action=ActionType.SEND_ALT_PAYMENT_LINK,
    rule=PolicyRule.LINK_CUSTOMER_ACTION_REQUIRED,
    status=ActionStatus.SIMULATED,
    settlement=SettlementSource.NONE,
    recovered_paise=0,
    provider_ref="dryrun_plink_1",
    diagnosis_source=DiagnosisSource.RULE_FALLBACK,
    boundary_violations=None,
    recoverability=None,
    **case_kwargs,
):
    case = make_case(payment_id=payment_id, amount_inr=amount_inr, reason=reason, **case_kwargs)
    recoverability = recoverability or case.recoverability
    diagnosis = Diagnosis(
        payment_id=case.payment_id, reason=case.failure_reason, recoverability=recoverability,
        source=diagnosis_source, boundary_violations=boundary_violations or [],
    )
    decision = Decision(
        payment_id=case.payment_id, amount_paise=case.amount_paise, reason=case.failure_reason,
        recoverability=recoverability, action=action, policy_rule=rule, reasoning="because",
    )
    outcome = ActionOutcome(
        payment_id=case.payment_id, action=action, status=status,
        provider_ref=provider_ref if action in {
            ActionType.SEND_ALT_PAYMENT_LINK, ActionType.RETRY_SAME_METHOD
        } else "",
        recovered_paise=recovered_paise, settlement_source=settlement,
    )
    return CaseRecord(case=case, diagnosis=diagnosis, decision=decision, outcome=outcome)


def stopped(payment_id, amount_inr, reason="fraud_suspected"):
    return record(
        payment_id=payment_id, amount_inr=amount_inr, reason=reason,
        action=ActionType.STOP_NO_ACTION, rule=PolicyRule.GATE_UNRECOVERABLE_CAUSE,
        status=ActionStatus.NO_ACTION, provider_ref="",
    )


def build(records, rejected=None, rows_read=None, **kwargs):
    rejected = rejected or []
    rows_read = rows_read if rows_read is not None else len(records) + len(rejected)
    return build_report(RUN, records, rejected, rows_read, **kwargs)


# --------------------------------------------------------------------------
# The central commitment: the two columns never merge
# --------------------------------------------------------------------------

def test_verified_and_modeled_are_reported_separately():
    report = build([
        record("pay_v", "1000.00", settlement=SettlementSource.VERIFIED_API,
               recovered_paise=100000, status=ActionStatus.EXECUTED, provider_ref="plink_1"),
        record("pay_m", "2000.00", settlement=SettlementSource.MODELED, recovered_paise=200000),
    ])
    assert report.recovered_verified.cases == 1
    assert report.recovered_verified.amount_paise == 100000
    assert report.recovered_modeled.cases == 1
    assert report.recovered_modeled.amount_paise == 200000


def test_no_field_anywhere_sums_verified_and_modeled():
    """
    Structural guard: walk the whole serialised report and fail on any number that
    equals verified + modeled.

    The amounts are chosen so the forbidden sum cannot collide with a legitimate
    figure. Verified recovers ₹700 of a ₹1000 case (Razorpay confirmed less than
    the full amount), modeled recovers ₹2000 in full, and a ₹5000 case is left
    untouched — so the sum is ₹2700 while every real total is 700, 1000, 2000,
    3000, 5000 or 8000.
    """
    report = build([
        record("pay_v", "1000.00", settlement=SettlementSource.VERIFIED_API,
               recovered_paise=70000, status=ActionStatus.EXECUTED, provider_ref="plink_1"),
        record("pay_m", "2000.00", settlement=SettlementSource.MODELED, recovered_paise=200000),
        record("pay_n", "5000.00"),
    ])
    assert report.recovered_verified.amount_paise == 70000
    assert report.recovered_modeled.amount_paise == 200000
    forbidden = {270000, 2700.0, 2700}
    payload = report.model_dump(mode="json")

    def walk(node, path=""):
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for i, value in enumerate(node):
                walk(value, f"{path}[{i}]")
        elif isinstance(node, (int, float)) and not isinstance(node, bool):
            assert node not in forbidden, (
                f"{path} = {node} looks like verified + modeled blended into one figure"
            )

    walk(payload)


def test_a_case_is_never_counted_in_both_settlement_columns():
    report = build([
        record(f"pay_{i}", "1000.00", settlement=SettlementSource.MODELED, recovered_paise=100000)
        for i in range(5)
    ])
    assert report.recovered_verified.cases == 0
    assert report.recovered_modeled.cases == 5
    assert report.reconciliation["settlement_columns_are_disjoint"]


def test_each_rate_has_its_own_denominator():
    report = build([
        record("pay_v", "1000.00", settlement=SettlementSource.VERIFIED_API,
               recovered_paise=100000, status=ActionStatus.EXECUTED, provider_ref="plink_1"),
        record("pay_m", "1000.00", settlement=SettlementSource.MODELED, recovered_paise=100000),
        record("pay_n", "2000.00"),
    ])
    # attempted = 4000; verified 1000 -> 25%, modeled 1000 -> 25%
    assert report.attempted.amount_paise == 400000
    assert report.recovery_rate_verified_pct == 25.0
    assert report.recovery_rate_modeled_pct == 25.0


# --------------------------------------------------------------------------
# The denominator cannot be gamed
# --------------------------------------------------------------------------

def test_the_recovery_denominator_is_money_attempted_not_money_at_risk():
    """
    Dividing by the whole batch would let the agent raise its own score by
    declining to chase things. Adding 100 untouched gated cases must not move the
    rate at all.
    """
    attempted = [record("pay_m", "1000.00", settlement=SettlementSource.MODELED,
                        recovered_paise=100000)]
    lean = build(attempted)
    padded = build(attempted + [stopped(f"pay_g{i}", "5000.00") for i in range(100)])

    assert lean.recovery_rate_modeled_pct == padded.recovery_rate_modeled_pct == 100.0
    assert padded.at_risk.amount_paise > lean.at_risk.amount_paise


def test_stopped_and_escalated_cases_are_not_attempted():
    report = build([
        stopped("pay_g", "1000.00"),
        record("pay_e", "1000.00", action=ActionType.ESCALATE_TO_HUMAN,
               rule=PolicyRule.ESCALATE_UNKNOWN_CAUSE, status=ActionStatus.ESCALATED,
               provider_ref=""),
    ])
    assert report.attempted.cases == 0
    assert report.recovery_rate_verified_pct == 0.0
    assert report.recovery_rate_modeled_pct == 0.0


def test_failed_actions_are_not_counted_as_attempted():
    report = build([
        record("pay_f", "1000.00", status=ActionStatus.FAILED, provider_ref=""),
    ])
    assert report.attempted.cases == 0


def test_an_empty_run_produces_a_valid_report():
    report = build([])
    assert report.at_risk.cases == 0
    assert report.recovery_rate_verified_pct == 0.0
    assert report.stop_compliance_pct == 100.0
    assert report.all_invariants_hold


# --------------------------------------------------------------------------
# Restraint counted as a result
# --------------------------------------------------------------------------

def test_gated_money_is_a_headline_figure():
    report = build([
        stopped("pay_g1", "1000.00", "fraud_suspected"),
        stopped("pay_g2", "2000.00", "card_blocked"),
        stopped("pay_g3", "3000.00", "customer_cancelled"),
        record("pay_ok", "500.00"),
    ])
    assert report.deliberately_not_chased.cases == 3
    assert report.deliberately_not_chased.amount_inr == 6000.00
    assert report.unrecoverable.amount_inr == 6000.00


def test_stop_compliance_is_100_when_every_gated_case_stops():
    report = build([stopped(f"pay_{i}", "1000.00") for i in range(8)])
    assert report.gated_cases == 8
    assert report.correctly_stopped_cases == 8
    assert report.stop_compliance_pct == 100.0
    assert report.reconciliation["every_gated_case_stopped"]


def test_stop_compliance_drops_and_invariants_fail_if_a_gated_case_is_touched():
    """The report must be capable of reporting failure, or 100% means nothing."""
    leaked = record(
        "pay_leak", "1000.00", reason="fraud_suspected",
        action=ActionType.SEND_ALT_PAYMENT_LINK, rule=PolicyRule.LINK_CUSTOMER_ACTION_REQUIRED,
        status=ActionStatus.EXECUTED, provider_ref="plink_LEAK",
        recoverability=Recoverability.UNRECOVERABLE,
    )
    report = build([stopped("pay_g", "1000.00"), leaked])

    assert report.gated_cases == 2
    assert report.correctly_stopped_cases == 1
    assert report.stop_compliance_pct == 50.0
    assert report.reconciliation["every_gated_case_stopped"] is False
    assert report.reconciliation["no_gated_case_was_contacted"] is False
    assert report.all_invariants_hold is False


def test_stop_compliance_is_100_when_there_is_nothing_to_gate():
    report = build([record("pay_ok", "1000.00")])
    assert report.gated_cases == 0
    assert report.stop_compliance_pct == 100.0


def test_withheld_money_is_reported_apart_from_gated_money():
    """Cooldown and retry-cap holds stay recoverable; the gate does not."""
    report = build([
        stopped("pay_g", "1000.00"),
        record("pay_c", "2000.00", action=ActionType.STOP_NO_ACTION,
               rule=PolicyRule.STOP_COOLDOWN_NOT_ELAPSED, status=ActionStatus.NO_ACTION,
               provider_ref=""),
        record("pay_r", "3000.00", action=ActionType.ESCALATE_TO_HUMAN,
               rule=PolicyRule.ESCALATE_RETRY_CAP_REACHED, status=ActionStatus.ESCALATED,
               provider_ref=""),
    ])
    assert report.deliberately_not_chased.amount_inr == 1000.00
    assert report.withheld_this_run.amount_inr == 5000.00


def test_escalations_are_broken_out_by_rule():
    report = build([
        record("pay_1", "100.00", action=ActionType.ESCALATE_TO_HUMAN,
               rule=PolicyRule.ESCALATE_UNKNOWN_CAUSE, status=ActionStatus.ESCALATED,
               provider_ref=""),
        record("pay_2", "100.00", action=ActionType.ESCALATE_TO_HUMAN,
               rule=PolicyRule.ESCALATE_NOT_CONTACTABLE, status=ActionStatus.ESCALATED,
               provider_ref=""),
        record("pay_3", "100.00", action=ActionType.ESCALATE_TO_HUMAN,
               rule=PolicyRule.ESCALATE_NOT_CONTACTABLE, status=ActionStatus.ESCALATED,
               provider_ref=""),
    ])
    assert report.escalations.cases == 3
    assert report.escalations_by_rule == {
        "escalate_unknown_cause": 1,
        "escalate_not_contactable": 2,
    }


def test_refused_llm_overreach_is_counted():
    report = build([
        record("pay_1", "100.00", boundary_violations=["refused to relabel", "bad action"]),
        record("pay_2", "100.00", boundary_violations=["cause downgraded"]),
        record("pay_3", "100.00"),
    ])
    assert report.boundary_violations_refused == 3


# --------------------------------------------------------------------------
# Dry run honesty
# --------------------------------------------------------------------------

def test_a_dry_run_reports_zero_real_contacts():
    report = build([record(f"pay_{i}", "100.00") for i in range(5)], dry_run=True)
    assert report.attempted.cases == 5
    assert report.customer_contacts_made == 0
    assert report.reconciliation["dry_run_made_no_contact"]


def test_a_live_run_counts_only_executed_contacts():
    report = build(
        [
            record("pay_1", "100.00", status=ActionStatus.EXECUTED, provider_ref="plink_1"),
            record("pay_2", "100.00", status=ActionStatus.SIMULATED),
            stopped("pay_3", "100.00"),
        ],
        dry_run=False,
    )
    assert report.customer_contacts_made == 1
    assert report.attempted.cases == 2


# --------------------------------------------------------------------------
# Exceptions are itemised
# --------------------------------------------------------------------------

def test_rejected_rows_appear_as_itemised_exceptions():
    rejected = [
        RejectedRecord(row_number=7, payment_id="", reject_reason=RejectReason.MISSING_PAYMENT_ID,
                       detail="payment_id is empty"),
        RejectedRecord(row_number=9, payment_id="pay_dup",
                       reject_reason=RejectReason.DUPLICATE_PAYMENT_ID, detail="already seen"),
    ]
    report = build([record("pay_1", "100.00")], rejected=rejected)

    kinds = [e.kind for e in report.unresolved_exceptions]
    assert kinds.count("rejected_row") == 2
    assert report.unresolved_exception_count == 2
    refs = {e.reference for e in report.unresolved_exceptions}
    assert "row 7" in refs and "pay_dup" in refs
    for item in report.unresolved_exceptions:
        assert item.detail, "every exception must explain itself"


def test_failed_api_calls_appear_as_itemised_exceptions():
    failed = record("pay_f", "1500.00", status=ActionStatus.FAILED, provider_ref="")
    failed = failed.model_copy(
        update={"outcome": failed.outcome.model_copy(update={"error": "Razorpay 500"})}
    )
    report = build([failed])

    item = report.unresolved_exceptions[0]
    assert item.kind == "action_failed"
    assert item.reference == "pay_f"
    assert "Razorpay 500" in item.detail
    assert item.amount_inr == 1500.00


# --------------------------------------------------------------------------
# Buckets, counters and self-checks
# --------------------------------------------------------------------------

def test_buckets_partition_the_batch():
    report = build([
        record("pay_1", "100.00", reason="insufficient_funds"),
        stopped("pay_2", "200.00"),
        record("pay_3", "300.00", reason="mystery_code",
               action=ActionType.ESCALATE_TO_HUMAN, rule=PolicyRule.ESCALATE_UNKNOWN_CAUSE,
               status=ActionStatus.ESCALATED, provider_ref=""),
    ])
    assert report.recoverable.amount_inr == 100.00
    assert report.unrecoverable.amount_inr == 200.00
    assert report.unknown_cause.amount_inr == 300.00
    assert report.at_risk.amount_inr == 600.00
    assert report.reconciliation["money_buckets_partition_the_batch"]
    assert report.reconciliation["case_buckets_partition_the_batch"]


def test_classification_follows_the_decision_not_the_csv():
    """A case narrowed by the diagnose stage is reported where it ended up."""
    narrowed = record(
        "pay_n", "1000.00", reason="bank_timeout", recoverability=Recoverability.UNRECOVERABLE,
        action=ActionType.STOP_NO_ACTION, rule=PolicyRule.GATE_UNRECOVERABLE_CAUSE,
        status=ActionStatus.NO_ACTION, provider_ref="",
    )
    report = build([narrowed])
    assert report.unrecoverable.cases == 1
    assert report.recoverable.cases == 0
    assert report.gated_cases == 1


def test_action_and_rule_counters_cover_every_case():
    records = [record(f"pay_{i}", "100.00") for i in range(4)] + [stopped("pay_g", "100.00")]
    report = build(records)
    assert sum(report.by_action.values()) == 5
    assert sum(report.by_policy_rule.values()) == 5
    assert report.reconciliation["every_case_has_exactly_one_action"]
    assert report.reconciliation["every_case_has_exactly_one_policy_rule"]


def test_diagnosis_sources_are_counted():
    report = build([
        record("pay_1", "100.00", diagnosis_source=DiagnosisSource.LLM),
        record("pay_2", "100.00", diagnosis_source=DiagnosisSource.LLM_CACHE),
        stopped("pay_3", "100.00"),
    ])
    assert report.by_diagnosis_source["llm"] == 1
    assert report.by_diagnosis_source["llm_cache"] == 1


def test_row_accounting_detects_a_lost_row():
    report = build([record("pay_1", "100.00")], rows_read=5)
    assert report.reconciliation["every_input_row_accounted_for"] is False


def test_a_partial_run_is_allowed_to_process_fewer_rows():
    report = build([record("pay_1", "100.00")], rows_read=90, partial_run=True)
    assert report.reconciliation["every_input_row_accounted_for"] is True
    assert report.partial_run is True


def test_recovery_without_an_action_is_flagged():
    """Money credited to a case the agent never acted on is a bug, and the report
    has to be able to say so."""
    cheating = stopped("pay_g", "1000.00")
    cheating = cheating.model_copy(
        update={"outcome": cheating.outcome.model_copy(update={
            "recovered_paise": 100000, "settlement_source": SettlementSource.MODELED
        })}
    )
    report = build([cheating])
    assert report.reconciliation["no_recovery_without_an_action"] is False
    assert report.all_invariants_hold is False


def test_all_invariants_hold_on_a_clean_report():
    report = build([
        record("pay_1", "100.00", settlement=SettlementSource.MODELED, recovered_paise=10000),
        record("pay_2", "200.00"),
        stopped("pay_3", "300.00"),
        record("pay_4", "400.00", action=ActionType.ESCALATE_TO_HUMAN,
               rule=PolicyRule.ESCALATE_RETRY_CAP_REACHED, status=ActionStatus.ESCALATED,
               provider_ref=""),
    ])
    assert report.all_invariants_hold, report.reconciliation


# --------------------------------------------------------------------------
# Money precision
# --------------------------------------------------------------------------

def test_totals_stay_exact_over_many_small_amounts():
    records = [record(f"pay_{i}", "0.10") for i in range(1000)]
    report = build(records)
    assert report.at_risk.amount_paise == 10_000
    assert report.at_risk.amount_inr == 100.00


# --------------------------------------------------------------------------
# Text rendering
# --------------------------------------------------------------------------

def test_text_report_states_the_separation_and_the_mode():
    report = build([
        record("pay_1", "100.00", settlement=SettlementSource.MODELED, recovered_paise=10000),
        stopped("pay_2", "200.00"),
    ], dry_run=True)
    text = render_text_report(report)

    assert "never added together" in text
    assert "DRY RUN" in text
    assert "verified (Razorpay confirmed)" in text
    assert "modeled (seeded model)" in text
    assert "deliberately not chased" in text
    assert "ALL INVARIANTS HOLD: True" in text


def test_text_report_flags_a_partial_run():
    report = build([record("pay_1", "100.00")], rows_read=90, partial_run=True)
    assert "PARTIAL RUN" in render_text_report(report)


def test_text_report_shows_failing_invariants():
    report = build([record("pay_1", "100.00")], rows_read=5)
    text = render_text_report(report)
    assert "FAIL  every_input_row_accounted_for" in text
    assert "ALL INVARIANTS HOLD: False" in text
