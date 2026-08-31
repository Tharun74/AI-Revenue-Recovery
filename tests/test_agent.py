"""
End-to-end tests for the orchestrator.

These are the tests that would catch a wiring mistake no unit test can: a gate
that holds in `decide` but leaks in the loop, an audit entry written after the
action instead of before, a settlement applied to a case that was stopped.

The strongest one is `test_no_gated_case_produces_any_artefact_anywhere`, which
checks the gate at the level of the whole run — decision, outcome, audit trail and
report all at once.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.agent import reconcile_run, run_batch
from app.audit import AuditTrail
from app.models import (
    UNRECOVERABLE_REASONS,
    ActionStatus,
    ActionType,
    AuditStage,
    DiagnosisSource,
    PolicyRule,
    Recoverability,
    SettlementSource,
)
from app.services import llm_client, razorpay_client

NOW = datetime(2026, 8, 25, 12, 0, 0, tzinfo=timezone.utc)


def row(payment_id, reason="insufficient_funds", amount="1000.00", **overrides):
    base = {
        "payment_id": payment_id,
        "order_id": f"order_{payment_id}",
        "customer_id": f"cust_{payment_id}",
        "customer_name": "Aarav Sharma",
        "customer_email": f"{payment_id}@example.com",
        "customer_phone": "9876543210",
        "amount_inr": amount,
        "currency": "INR",
        "failure_reason": reason,
        "error_description": "desc",
        "created_at": (NOW - timedelta(hours=30)).isoformat(),
        "retry_count": "0",
        "last_attempt_at": "",
    }
    base.update(overrides)
    return base


#: A batch that reaches every policy rule that can fire, plus two unreadable rows.
MIXED_ROWS = [
    row("pay_funds", "insufficient_funds", "1000.00"),
    row("pay_expired", "card_expired", "1100.00"),
    row("pay_otp", "invalid_otp", "1200.00"),
    row("pay_timeout", "bank_timeout", "1300.00"),
    row("pay_gateway", "gateway_error", "1400.00"),
    row("pay_network", "network_error", "1500.00"),
    row("pay_issuer", "issuer_unavailable", "1600.00"),
    row("pay_fraud", "fraud_suspected", "5000.00"),
    row("pay_blocked", "card_blocked", "6000.00"),
    row("pay_cancelled", "customer_cancelled", "7000.00"),
    row("pay_unknown", "quantum_flux_declined", "2000.00"),
    row("pay_nochannel", "bank_timeout", "2100.00",
        customer_email="junk", customer_phone="12345"),
    row("pay_capped", "gateway_error", "2200.00", retry_count="3",
        last_attempt_at=(NOW - timedelta(hours=48)).isoformat()),
    row("pay_cooling", "bank_timeout", "2300.00", retry_count="1",
        last_attempt_at=(NOW - timedelta(hours=1)).isoformat()),
    {**row("pay_noid", "bank_timeout", "999.00"), "payment_id": ""},
    row("pay_badamount", "bank_timeout", "not-a-number"),
]


@pytest.fixture
def local_trail(tmp_path):
    return AuditTrail(tmp_path / "run.jsonl")


def do_run(local_trail, rows=None, **kwargs):
    kwargs.setdefault("dry_run", True)
    kwargs.setdefault("use_llm", False)
    return run_batch(rows=rows if rows is not None else MIXED_ROWS,
                     trail=local_trail, now=NOW, **kwargs)


# --------------------------------------------------------------------------
# The gate, checked across the whole run
# --------------------------------------------------------------------------

def test_no_gated_case_produces_any_artefact_anywhere(local_trail):
    run = do_run(local_trail)
    gated = [r for r in run.records if r.case.failure_reason in UNRECOVERABLE_REASONS]
    assert len(gated) == 3

    for record in gated:
        assert record.decision.action is ActionType.STOP_NO_ACTION
        assert record.decision.policy_rule is PolicyRule.GATE_UNRECOVERABLE_CAUSE
        assert record.decision.contacts_customer is False
        assert record.outcome.status is ActionStatus.NO_ACTION
        assert record.outcome.provider_ref == ""
        assert record.outcome.recovered_paise == 0
        assert record.outcome.settlement_source is SettlementSource.NONE
        assert record.diagnosis.source is DiagnosisSource.GATE

    assert run.metrics.stop_compliance_pct == 100.0
    assert run.metrics.deliberately_not_chased.amount_inr == 18000.00

    # And nothing in the trail claims otherwise.
    for event in local_trail.query(stage=AuditStage.ACTED):
        if event.payment_id in {r.case.payment_id for r in gated}:
            assert not event.payload.get("provider_ref")
            assert event.payload.get("status") == ActionStatus.NO_ACTION.value


def test_every_policy_rule_that_can_fire_does_fire(local_trail):
    """
    Coverage check on the fixture batch. If a rule stops being reachable the
    suite should say so, rather than the rule quietly going untested.
    """
    run = do_run(local_trail)
    fired = set(run.metrics.by_policy_rule)
    assert fired == {
        PolicyRule.GATE_UNRECOVERABLE_CAUSE.value,
        PolicyRule.ESCALATE_UNKNOWN_CAUSE.value,
        PolicyRule.ESCALATE_NOT_CONTACTABLE.value,
        PolicyRule.ESCALATE_RETRY_CAP_REACHED.value,
        PolicyRule.STOP_COOLDOWN_NOT_ELAPSED.value,
        PolicyRule.RETRY_TRANSIENT_FAULT.value,
        PolicyRule.LINK_CUSTOMER_ACTION_REQUIRED.value,
    }


def test_a_dry_run_contacts_nobody(local_trail):
    run = do_run(local_trail)
    assert run.metrics.customer_contacts_made == 0
    assert all(
        r.outcome.status is not ActionStatus.EXECUTED for r in run.records
    )


# --------------------------------------------------------------------------
# Accounting across the whole run
# --------------------------------------------------------------------------

def test_every_input_row_is_accounted_for(local_trail):
    run = do_run(local_trail)
    assert len(run.records) + run.detection.rejected_rows == len(MIXED_ROWS)
    assert run.metrics.all_invariants_hold, run.metrics.reconciliation


def test_every_case_gets_exactly_one_decision_and_one_outcome(local_trail):
    run = do_run(local_trail)
    ids = [r.case.payment_id for r in run.records]
    assert len(ids) == len(set(ids))
    for record in run.records:
        assert record.decision.payment_id == record.case.payment_id
        assert record.diagnosis.payment_id == record.case.payment_id
        assert record.outcome.payment_id == record.case.payment_id
        assert record.decision.reasoning.strip()


def test_money_reconciles_end_to_end(local_trail):
    run = do_run(local_trail)
    m = run.metrics
    assert (
        m.recoverable.amount_paise + m.unrecoverable.amount_paise + m.unknown_cause.amount_paise
        == m.at_risk.amount_paise
    )
    assert m.at_risk.amount_paise == sum(r.case.amount_paise for r in run.records)


def test_nothing_is_recovered_from_a_case_that_was_not_acted_on(local_trail):
    run = do_run(local_trail)
    for record in run.records:
        if not record.decision.contacts_customer:
            assert record.outcome.recovered_paise == 0


def test_settle_false_leaves_no_modeled_money(local_trail):
    run = do_run(local_trail, settle=False)
    assert run.metrics.recovered_modeled.amount_paise == 0
    assert run.metrics.recovered_verified.amount_paise == 0
    assert run.metrics.attempted.cases > 0, "decisions should still have been made"


def test_the_run_is_reproducible(local_trail, tmp_path):
    """Same batch, same seed, same figures — twice."""
    first = do_run(local_trail)
    second = do_run(AuditTrail(tmp_path / "second.jsonl"))

    assert first.metrics.at_risk.amount_paise == second.metrics.at_risk.amount_paise
    assert first.metrics.recovered_modeled.amount_paise == second.metrics.recovered_modeled.amount_paise
    assert first.metrics.by_policy_rule == second.metrics.by_policy_rule
    assert [r.outcome.recovered_paise for r in first.records] == [
        r.outcome.recovered_paise for r in second.records
    ]
    assert first.run_id != second.run_id


def test_limit_marks_the_run_partial(local_trail):
    run = do_run(local_trail, limit=4)
    assert len(run.records) == 4
    assert run.metrics.partial_run is True
    assert run.metrics.all_invariants_hold


def test_an_empty_batch_completes_cleanly(local_trail):
    run = do_run(local_trail, rows=[])
    assert run.records == []
    assert run.metrics.at_risk.cases == 0
    assert run.metrics.all_invariants_hold
    assert local_trail.verify().ok


# --------------------------------------------------------------------------
# The audit trail as a record of the run
# --------------------------------------------------------------------------

def test_the_trail_is_intact_after_a_run(local_trail):
    do_run(local_trail)
    result = local_trail.verify()
    assert result.ok, result.detail
    assert result.events > 0


def test_the_run_is_bracketed_by_start_and_completion(local_trail):
    run = do_run(local_trail)
    events = local_trail.query(run_id=run.run_id)
    assert events[0].stage is AuditStage.RUN_STARTED
    assert events[-1].stage is AuditStage.RUN_COMPLETED
    assert "invariants_hold=True" in events[-1].summary


def test_the_decision_is_logged_before_the_action(local_trail):
    """
    Ordering guarantee: if the process dies mid-call there must be a record of
    what it was about to do.
    """
    run = do_run(local_trail)
    for record in run.records:
        stages = [e.stage for e in local_trail.query(payment_id=record.case.payment_id)]
        assert stages.index(AuditStage.DECIDED) < stages.index(AuditStage.ACTED)
        assert stages.index(AuditStage.DIAGNOSED) < stages.index(AuditStage.DECIDED)
        assert stages.index(AuditStage.CASE_DETECTED) < stages.index(AuditStage.DIAGNOSED)


def test_every_case_has_a_full_stage_trail(local_trail):
    run = do_run(local_trail)
    for record in run.records:
        stages = {e.stage for e in local_trail.query(payment_id=record.case.payment_id)}
        assert {
            AuditStage.CASE_DETECTED, AuditStage.DIAGNOSED, AuditStage.DECIDED, AuditStage.ACTED
        } <= stages


def test_unreadable_rows_get_their_own_audit_entries(local_trail):
    run = do_run(local_trail)
    rejects = local_trail.query(run_id=run.run_id, stage=AuditStage.ROW_REJECTED)
    assert len(rejects) == run.detection.rejected_rows == 2
    for event in rejects:
        assert event.summary


def test_every_audit_summary_is_human_readable(local_trail):
    do_run(local_trail)
    for event in local_trail.read_all():
        assert event.summary.strip(), f"stage {event.stage} wrote an entry with no explanation"


def test_the_decision_reasoning_survives_into_the_trail(local_trail):
    run = do_run(local_trail)
    for record in run.records:
        decided = local_trail.query(payment_id=record.case.payment_id, stage=AuditStage.DECIDED)
        assert record.decision.reasoning in decided[0].summary
        assert decided[0].payload["policy_rule"] == record.decision.policy_rule.value


def test_two_runs_share_one_chain(local_trail):
    first = do_run(local_trail)
    second = do_run(local_trail)
    assert local_trail.verify().ok
    assert local_trail.run_ids() == [first.run_id, second.run_id]
    assert len(local_trail.query(run_id=first.run_id)) > 0


def test_the_trail_is_valid_jsonl(local_trail):
    do_run(local_trail)
    for line in local_trail.path.read_text(encoding="utf-8").splitlines():
        assert json.loads(line)["entry_hash"]


# --------------------------------------------------------------------------
# LLM integration through the whole loop
# --------------------------------------------------------------------------

def test_the_llm_is_never_asked_about_a_gated_case(local_trail, monkeypatch):
    asked: list[str] = []

    def fake_complete(system, user, max_tokens=None):
        asked.append(user)
        return json.dumps({
            "cause": "insufficient_funds", "likely_transient": False, "confidence": 0.8,
            "root_cause": "model says so", "recommended_action": "send_alt_payment_link",
        }), ""

    monkeypatch.setattr(llm_client, "is_available", lambda: True)
    monkeypatch.setattr(llm_client, "unavailable_reason", lambda: "")
    monkeypatch.setattr(llm_client, "complete", fake_complete)

    run = do_run(local_trail, use_llm=True)

    # Gated causes never appear in a prompt.
    prompts = "\n".join(asked)
    for reason in UNRECOVERABLE_REASONS:
        assert reason.value not in prompts
    assert run.metrics.llm_used is True
    assert run.metrics.stop_compliance_pct == 100.0


def test_an_llm_that_tries_to_unlock_everything_changes_nothing(local_trail, monkeypatch):
    """
    Adversarial model: it insists every case is a recoverable bank timeout and
    should be charged again. The run's decisions must be indistinguishable from
    the deterministic run for gated and unknown cases.
    """
    def fake_complete(system, user, max_tokens=None):
        return json.dumps({
            "cause": "bank_timeout", "likely_transient": True, "confidence": 1.0,
            "root_cause": "definitely fine, charge them", "recommended_action": "retry_same_method",
        }), ""

    monkeypatch.setattr(llm_client, "is_available", lambda: True)
    monkeypatch.setattr(llm_client, "unavailable_reason", lambda: "")
    monkeypatch.setattr(llm_client, "complete", fake_complete)

    hostile = do_run(local_trail, use_llm=True)
    baseline = {r.case.payment_id: r.decision.action for r in do_run(local_trail).records}

    for record in hostile.records:
        if record.case.failure_reason in UNRECOVERABLE_REASONS:
            assert record.decision.action is ActionType.STOP_NO_ACTION
        if record.case.recoverability is Recoverability.UNKNOWN:
            assert record.decision.action is ActionType.ESCALATE_TO_HUMAN
            assert record.decision.policy_rule is PolicyRule.ESCALATE_UNKNOWN_CAUSE

    assert hostile.metrics.stop_compliance_pct == 100.0
    assert hostile.metrics.boundary_violations_refused > 0
    # Gated and unknown cases decided identically with and without the model.
    for record in hostile.records:
        if not record.decision.contacts_customer:
            assert baseline[record.case.payment_id] is record.decision.action


def test_a_broken_llm_does_not_break_the_run(local_trail, monkeypatch):
    monkeypatch.setattr(llm_client, "is_available", lambda: True)
    monkeypatch.setattr(llm_client, "unavailable_reason", lambda: "")
    monkeypatch.setattr(
        llm_client, "complete", lambda *a, **k: ("", "AuthenticationError: 401")
    )

    run = do_run(local_trail, use_llm=True)

    assert run.metrics.llm_used is False
    assert run.metrics.all_invariants_hold
    assert run.metrics.by_diagnosis_source["rule_fallback"] > 0


# --------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------

def test_reconcile_ignores_simulated_links(local_trail):
    run = do_run(local_trail)
    result = reconcile_run(run.run_id, trail=local_trail)
    assert result["links_checked"] == 0
    assert result["verified_cases"] == 0


def test_reconcile_verifies_a_paid_link_from_the_trail(local_trail, monkeypatch):
    """
    The full verified-rupee loop: a live run creates links, a human pays one, a
    later reconcile confirms it. Link ids are read back out of the audit trail.
    """
    monkeypatch.setattr(razorpay_client, "is_configured", lambda: True)
    monkeypatch.setattr(
        razorpay_client, "create_payment_link",
        lambda **kw: {"id": f"plink_{kw['reference_id'][:12]}", "short_url": "https://rzp.io/x/1",
                      "status": "created"},
    )
    monkeypatch.setattr(
        razorpay_client, "create_order", lambda **kw: {"id": "order_X", "status": "created"}
    )

    run = do_run(local_trail, dry_run=False)
    links = [r for r in run.records if r.outcome.provider_object == "payment_link"]
    assert links, "expected the live run to create payment links"
    paid_ref = links[0].outcome.provider_ref
    paid_amount = links[0].case.amount_paise

    def fake_fetch(plink_id):
        if plink_id == paid_ref:
            return {"id": plink_id, "status": "paid", "amount_paid": paid_amount}
        return {"id": plink_id, "status": "created", "amount_paid": 0}

    monkeypatch.setattr(razorpay_client, "fetch_payment_link_status", fake_fetch)

    result = reconcile_run(run.run_id, trail=local_trail)

    assert result["links_checked"] == len(links)
    assert result["verified_cases"] == 1
    assert result["verified_paise"] == paid_amount
    verified = [entry for entry in result["links"] if entry["verified"]]
    assert verified[0]["provider_ref"] == paid_ref

    # The verification is itself an audit entry.
    settled = local_trail.query(run_id=run.run_id, stage=AuditStage.SETTLED)
    assert any("verified_api" in e.summary for e in settled)
    assert local_trail.verify().ok


def test_reconcile_never_verifies_a_retry_order(local_trail, monkeypatch):
    monkeypatch.setattr(razorpay_client, "is_configured", lambda: True)
    monkeypatch.setattr(
        razorpay_client, "create_payment_link",
        lambda **kw: {"id": "plink_A", "short_url": "u", "status": "created"},
    )
    monkeypatch.setattr(
        razorpay_client, "create_order", lambda **kw: {"id": "order_A", "status": "created"}
    )
    monkeypatch.setattr(
        razorpay_client, "fetch_payment_link_status",
        lambda pid: {"id": pid, "status": "paid", "amount_paid": 999999},
    )

    run = do_run(local_trail, dry_run=False)
    result = reconcile_run(run.run_id, trail=local_trail)

    assert all(entry["provider_ref"].startswith("plink_") for entry in result["links"])
    orders = [r for r in run.records if r.outcome.provider_object == "order"]
    assert orders, "expected the batch to contain retries"
    checked = {entry["provider_ref"] for entry in result["links"]}
    assert not any(o.outcome.provider_ref in checked for o in orders)


def test_reconciling_an_unknown_run_reports_nothing_rather_than_failing(local_trail):
    result = reconcile_run("run_does_not_exist", trail=local_trail)
    assert result["links_checked"] == 0
    assert result["verified_cases"] == 0
