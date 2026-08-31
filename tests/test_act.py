"""
Tests for the act stage.

What is being pinned down here:

* Stopping and escalating are implemented as *nothing happening* — no call, no
  provider reference, no recovered rupee.
* A dry run cannot reach the network. The `block_live_calls` fixture in
  conftest.py makes any attempt an error, so these tests prove it rather than
  assert it.
* A Razorpay failure is captured, never raised. One 500 must not abort a batch.
* The settlement model is deterministic, and it can never overwrite a figure that
  Razorpay confirmed.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app import act
from app.detect import normalize_row
from app.diagnose import rule_diagnosis
from app.models import (
    ActionOutcome,
    ActionStatus,
    ActionType,
    Decision,
    DiagnosisSource,
    FailureReason,
    PolicyRule,
    Recoverability,
    SettlementSource,
)
from app.services import razorpay_client

NOW = datetime(2026, 8, 25, 12, 0, 0, tzinfo=timezone.utc)
RUN_ID = "run_20260825120000_abcdef"


def make_case(**overrides):
    row = {
        "payment_id": "pay_act_0001",
        "order_id": "order_act_0001",
        "customer_id": "cust_act_1",
        "customer_name": "Aarav Sharma",
        "customer_email": "aarav@example.com",
        "customer_phone": "9876543210",
        "amount_inr": "1499.00",
        "currency": "INR",
        "failure_reason": "insufficient_funds",
        "error_description": "Insufficient balance",
        "created_at": (NOW - timedelta(hours=30)).isoformat(),
        "retry_count": "0",
        "last_attempt_at": "",
    }
    row.update(overrides)
    case, rejection = normalize_row(1, row, set(), now=NOW)
    assert rejection is None, rejection
    return case


def make_decision(case, action, rule=PolicyRule.LINK_CUSTOMER_ACTION_REQUIRED, **overrides):
    fields = dict(
        payment_id=case.payment_id,
        amount_paise=case.amount_paise,
        reason=case.failure_reason,
        recoverability=case.recoverability,
        action=action,
        policy_rule=rule,
        reasoning="test decision",
        contact_channel="email+sms",
    )
    fields.update(overrides)
    return Decision(**fields)


@pytest.fixture
def fake_razorpay(monkeypatch):
    """Records calls so tests can assert on exactly what was sent."""
    calls: list[tuple[str, dict]] = []

    def install(link=None, order=None, link_error=None, order_error=None, fetch=None,
                fetch_error=None):
        def create_payment_link(**kwargs):
            calls.append(("payment_link", kwargs))
            if link_error:
                raise link_error
            return link or {"id": "plink_TEST123", "short_url": "https://rzp.io/x/abc",
                            "status": "created"}

        def create_order(**kwargs):
            calls.append(("order", kwargs))
            if order_error:
                raise order_error
            return order or {"id": "order_TEST123", "status": "created"}

        def fetch_payment_link_status(plink_id):
            calls.append(("fetch", {"plink_id": plink_id}))
            if fetch_error:
                raise fetch_error
            return fetch or {"id": plink_id, "status": "created", "amount_paid": 0}

        monkeypatch.setattr(razorpay_client, "is_configured", lambda: True)
        monkeypatch.setattr(razorpay_client, "create_payment_link", create_payment_link)
        monkeypatch.setattr(razorpay_client, "create_order", create_order)
        monkeypatch.setattr(razorpay_client, "fetch_payment_link_status", fetch_payment_link_status)
        return calls

    return install


# --------------------------------------------------------------------------
# The two non-actions
# --------------------------------------------------------------------------

def test_stop_makes_no_call_and_leaves_no_trace():
    case = make_case(failure_reason="fraud_suspected")
    decision = make_decision(case, ActionType.STOP_NO_ACTION, PolicyRule.GATE_UNRECOVERABLE_CAUSE)

    outcome = act.execute(case, decision, run_id=RUN_ID, dry_run=False)

    assert outcome.status is ActionStatus.NO_ACTION
    assert outcome.provider_ref == ""
    assert outcome.provider_short_url == ""
    assert outcome.recovered_paise == 0
    assert outcome.settlement_source is SettlementSource.NONE
    assert "gate_unrecoverable_cause" in outcome.detail


def test_escalation_makes_no_call_and_says_no_contact_was_made():
    case = make_case()
    decision = make_decision(case, ActionType.ESCALATE_TO_HUMAN, PolicyRule.ESCALATE_UNKNOWN_CAUSE)

    outcome = act.execute(case, decision, run_id=RUN_ID, dry_run=False)

    assert outcome.status is ActionStatus.ESCALATED
    assert outcome.provider_ref == ""
    assert "No customer contact was made" in outcome.detail


# --------------------------------------------------------------------------
# Dry run: nothing leaves the process
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "action", [ActionType.SEND_ALT_PAYMENT_LINK, ActionType.RETRY_SAME_METHOD]
)
def test_dry_run_never_touches_the_network(action):
    """
    conftest's block_live_calls turns any real client construction into an error,
    so reaching the network here would fail the test rather than pass silently.
    """
    case = make_case()
    outcome = act.execute(case, make_decision(case, action), run_id=RUN_ID, dry_run=True)

    assert outcome.status is ActionStatus.SIMULATED
    assert outcome.provider_ref.startswith("dryrun_")
    assert "DRY RUN" in outcome.detail
    assert outcome.recovered_paise == 0


def test_dry_run_refs_are_distinguishable_from_real_ones():
    case = make_case()
    link = act.execute(
        case, make_decision(case, ActionType.SEND_ALT_PAYMENT_LINK), run_id=RUN_ID, dry_run=True
    )
    order = act.execute(
        case, make_decision(case, ActionType.RETRY_SAME_METHOD), run_id=RUN_ID, dry_run=True
    )
    assert link.provider_object == "payment_link"
    assert order.provider_object == "order"
    assert link.provider_ref != order.provider_ref


# --------------------------------------------------------------------------
# Live path
# --------------------------------------------------------------------------

def test_payment_link_is_created_with_exact_integer_paise(fake_razorpay):
    calls = fake_razorpay()
    case = make_case(amount_inr="1003.21")
    decision = make_decision(case, ActionType.SEND_ALT_PAYMENT_LINK)

    outcome = act.execute(case, decision, run_id=RUN_ID, dry_run=False)

    kind, kwargs = calls[0]
    assert kind == "payment_link"
    assert kwargs["amount_paise"] == 100321
    assert isinstance(kwargs["amount_paise"], int)
    assert outcome.status is ActionStatus.EXECUTED
    assert outcome.provider_ref == "plink_TEST123"
    assert outcome.provider_short_url == "https://rzp.io/x/abc"


def test_link_reference_id_is_scoped_to_the_run(fake_razorpay):
    """Razorpay enforces reference_id uniqueness, so two runs over the same batch
    must not collide."""
    calls = fake_razorpay()
    case = make_case()
    act.execute(case, make_decision(case, ActionType.SEND_ALT_PAYMENT_LINK),
                run_id="run_AAAAAAAA_1", dry_run=False)
    act.execute(case, make_decision(case, ActionType.SEND_ALT_PAYMENT_LINK),
                run_id="run_BBBBBBBB_2", dry_run=False)
    refs = [kwargs["reference_id"] for _, kwargs in calls]
    assert refs[0] != refs[1]
    assert all(r.startswith(case.payment_id) for r in refs)


def test_link_notes_carry_the_audit_metadata(fake_razorpay):
    calls = fake_razorpay()
    case = make_case()
    decision = make_decision(case, ActionType.SEND_ALT_PAYMENT_LINK)
    act.execute(case, decision, run_id=RUN_ID, dry_run=False)

    notes = calls[0][1]["notes"]
    assert notes["payment_id"] == case.payment_id
    assert notes["failure_reason"] == case.failure_reason.value
    assert notes["policy_rule"] == decision.policy_rule.value
    assert notes["agent_run_id"] == RUN_ID


def test_unusable_channels_are_not_sent_to_razorpay(fake_razorpay):
    """A malformed phone number must not be handed over as if it were a contact."""
    calls = fake_razorpay()
    case = make_case(customer_phone="12345")
    act.execute(case, make_decision(case, ActionType.SEND_ALT_PAYMENT_LINK),
                run_id=RUN_ID, dry_run=False)
    assert calls[0][1]["customer_phone"] == ""
    assert calls[0][1]["customer_email"] == "aarav@example.com"


def test_retry_creates_an_order_not_a_link(fake_razorpay):
    calls = fake_razorpay()
    case = make_case(failure_reason="bank_timeout")
    outcome = act.execute(
        case, make_decision(case, ActionType.RETRY_SAME_METHOD, PolicyRule.RETRY_TRANSIENT_FAULT),
        run_id=RUN_ID, dry_run=False,
    )
    assert [kind for kind, _ in calls] == ["order"]
    assert outcome.provider_object == "order"
    assert outcome.provider_ref == "order_TEST123"
    # The limitation is stated on the outcome itself, not buried in a doc.
    assert "never verified" in outcome.detail


def test_customer_facing_description_leaks_no_internal_jargon():
    case = make_case(failure_reason="fraud_suspected")
    text = act.link_description(case)
    assert "fraud" not in text.lower()
    assert case.order_id in text
    assert "1,499.00" in text


# --------------------------------------------------------------------------
# Failures are captured, not raised
# --------------------------------------------------------------------------

def test_link_failure_is_recorded_rather_than_raised(fake_razorpay):
    fake_razorpay(link_error=RuntimeError("Razorpay is having a moment"))
    case = make_case()

    outcome = act.execute(
        case, make_decision(case, ActionType.SEND_ALT_PAYMENT_LINK), run_id=RUN_ID, dry_run=False
    )

    assert outcome.status is ActionStatus.FAILED
    assert "Razorpay is having a moment" in outcome.error
    assert outcome.recovered_paise == 0
    assert outcome.settlement_source is SettlementSource.NONE


def test_order_failure_is_recorded_rather_than_raised(fake_razorpay):
    fake_razorpay(order_error=RuntimeError("bad request"))
    case = make_case(failure_reason="bank_timeout")
    outcome = act.execute(
        case, make_decision(case, ActionType.RETRY_SAME_METHOD), run_id=RUN_ID, dry_run=False
    )
    assert outcome.status is ActionStatus.FAILED
    assert "bad request" in outcome.error


def test_live_action_without_keys_fails_closed(monkeypatch):
    monkeypatch.setattr(razorpay_client, "is_configured", lambda: False)
    case = make_case()
    outcome = act.execute(
        case, make_decision(case, ActionType.SEND_ALT_PAYMENT_LINK), run_id=RUN_ID, dry_run=False
    )
    assert outcome.status is ActionStatus.FAILED
    assert "not configured" in outcome.error


# --------------------------------------------------------------------------
# The seeded outcome model
# --------------------------------------------------------------------------

def test_the_draw_is_stable_across_calls_and_processes():
    """
    Uses sha256 rather than hash(), so the value cannot shift with
    PYTHONHASHSEED. These are the literal expected values, which is the only way
    to catch an accidental switch back to hash().
    """
    assert act.deterministic_draw("pay_abc", seed=42) == act.deterministic_draw("pay_abc", seed=42)
    assert act.deterministic_draw("pay_abc", seed=42) != act.deterministic_draw("pay_abd", seed=42)
    assert act.deterministic_draw("pay_abc", seed=1) != act.deterministic_draw("pay_abc", seed=2)
    assert 0.0 <= act.deterministic_draw("pay_abc", seed=42) < 1.0


def test_draw_distribution_is_roughly_uniform():
    draws = [act.deterministic_draw(f"pay_{i}", seed=42) for i in range(2000)]
    assert 0.47 < sum(draws) / len(draws) < 0.53


def test_prior_attempts_reduce_the_modeled_probability():
    zero = act.modeled_probability(make_case(retry_count="0"), FailureReason.BANK_TIMEOUT)
    one = act.modeled_probability(make_case(retry_count="1"), FailureReason.BANK_TIMEOUT)
    two = act.modeled_probability(make_case(retry_count="2"), FailureReason.BANK_TIMEOUT)
    assert zero > one > two


def test_every_recoverable_cause_has_a_modeled_probability():
    from app.models import RECOVERABLE_REASONS

    for reason in RECOVERABLE_REASONS:
        assert reason in act.BASE_RECOVERY_PROBABILITY, reason.value
        assert 0 < act.BASE_RECOVERY_PROBABILITY[reason] < 1


def test_gated_causes_have_no_recovery_probability():
    from app.models import UNRECOVERABLE_REASONS

    for reason in UNRECOVERABLE_REASONS:
        assert reason not in act.BASE_RECOVERY_PROBABILITY


def test_settlement_recovers_the_exact_case_amount():
    """Partial credit would be a modelling choice nobody asked for; a settled case
    settles for what it was worth."""
    case = make_case(amount_inr="1003.21")
    decision = make_decision(case, ActionType.SEND_ALT_PAYMENT_LINK)
    outcome = act.execute(case, decision, run_id=RUN_ID, dry_run=True)

    settled = act.apply_modeled_settlement(case, decision, outcome, seed=0)
    if settled.settlement_source is SettlementSource.MODELED:
        assert settled.recovered_paise == 100321
    else:
        assert settled.recovered_paise == 0


@pytest.mark.parametrize(
    "status",
    [ActionStatus.NO_ACTION, ActionStatus.ESCALATED, ActionStatus.FAILED],
)
def test_the_model_never_credits_a_case_that_was_not_acted_on(status):
    case = make_case()
    decision = make_decision(case, ActionType.STOP_NO_ACTION)
    outcome = ActionOutcome(
        payment_id=case.payment_id, action=decision.action, status=status,
        settlement_source=SettlementSource.NONE,
    )
    settled = act.apply_modeled_settlement(case, decision, outcome)
    assert settled.recovered_paise == 0
    assert settled.settlement_source is SettlementSource.NONE


def test_the_model_never_overwrites_a_verified_figure():
    case = make_case()
    decision = make_decision(case, ActionType.SEND_ALT_PAYMENT_LINK)
    verified = ActionOutcome(
        payment_id=case.payment_id, action=decision.action, status=ActionStatus.EXECUTED,
        provider_object="payment_link", provider_ref="plink_REAL",
        recovered_paise=case.amount_paise, settlement_source=SettlementSource.VERIFIED_API,
        settlement_detail="VERIFIED: paid",
    )
    settled = act.apply_modeled_settlement(case, decision, verified)
    assert settled.settlement_source is SettlementSource.VERIFIED_API
    assert settled.settlement_detail == "VERIFIED: paid"


def test_settlement_detail_shows_the_arithmetic():
    case = make_case()
    decision = make_decision(case, ActionType.SEND_ALT_PAYMENT_LINK)
    outcome = act.execute(case, decision, run_id=RUN_ID, dry_run=True)
    settled = act.apply_modeled_settlement(case, decision, outcome)
    assert "p=" in settled.settlement_detail
    assert "draw=" in settled.settlement_detail
    assert "prior attempt" in settled.settlement_detail


# --------------------------------------------------------------------------
# Reconciliation: the only route to a verified rupee
# --------------------------------------------------------------------------

def _executed_link(case, ref="plink_REAL", modeled_paise=0):
    return ActionOutcome(
        payment_id=case.payment_id,
        action=ActionType.SEND_ALT_PAYMENT_LINK,
        status=ActionStatus.EXECUTED,
        provider_object="payment_link",
        provider_ref=ref,
        recovered_paise=modeled_paise,
        settlement_source=SettlementSource.MODELED if modeled_paise else SettlementSource.NONE,
        settlement_detail="Modeled outcome: ...",
    )


def test_a_paid_link_becomes_verified(fake_razorpay):
    case = make_case()
    fake_razorpay(fetch={"id": "plink_REAL", "status": "paid", "amount_paid": 149900})

    result = act.reconcile(_executed_link(case))

    assert result.settlement_source is SettlementSource.VERIFIED_API
    assert result.recovered_paise == 149900
    assert "VERIFIED" in result.settlement_detail


def test_verification_uses_the_amount_razorpay_reports(fake_razorpay):
    """Not the amount we hoped for — the amount the API actually confirms."""
    case = make_case(amount_inr="1499.00")
    fake_razorpay(fetch={"id": "plink_REAL", "status": "paid", "amount_paid": 100000})
    result = act.reconcile(_executed_link(case))
    assert result.recovered_paise == 100000


def test_an_unpaid_link_keeps_its_modeled_figure(fake_razorpay):
    case = make_case()
    fake_razorpay(fetch={"id": "plink_REAL", "status": "created", "amount_paid": 0})

    result = act.reconcile(_executed_link(case, modeled_paise=149900))

    assert result.settlement_source is SettlementSource.MODELED
    assert result.recovered_paise == 149900
    assert "link is 'created'" in result.settlement_detail


def test_a_paid_status_with_zero_amount_is_not_verified(fake_razorpay):
    case = make_case()
    fake_razorpay(fetch={"id": "plink_REAL", "status": "paid", "amount_paid": 0})
    result = act.reconcile(_executed_link(case))
    assert result.settlement_source is not SettlementSource.VERIFIED_API


def test_a_simulated_link_is_never_reconciled(fake_razorpay):
    calls = fake_razorpay()
    case = make_case()
    result = act.reconcile(_executed_link(case, ref="dryrun_plink_xyz"))
    assert calls == []
    assert result.settlement_source is not SettlementSource.VERIFIED_API
    assert "simulated" in result.settlement_detail


def test_an_order_is_never_reconcilable(fake_razorpay):
    """The stated consequence of implementing retry as an Order: no retry rupee is
    ever verifiable."""
    calls = fake_razorpay()
    case = make_case()
    order_outcome = ActionOutcome(
        payment_id=case.payment_id, action=ActionType.RETRY_SAME_METHOD,
        status=ActionStatus.EXECUTED, provider_object="order", provider_ref="order_REAL",
        settlement_source=SettlementSource.MODELED, recovered_paise=149900,
    )
    result = act.reconcile(order_outcome)
    assert calls == []
    assert result.settlement_source is SettlementSource.MODELED


def test_reconcile_failure_is_reported_not_raised(fake_razorpay):
    case = make_case()
    fake_razorpay(fetch_error=RuntimeError("network down"))
    result = act.reconcile(_executed_link(case))
    assert result.settlement_source is not SettlementSource.VERIFIED_API
    assert "network down" in result.settlement_detail


def test_reconcile_without_keys_reports_rather_than_guessing(monkeypatch):
    monkeypatch.setattr(razorpay_client, "is_configured", lambda: False)
    result = act.reconcile(_executed_link(make_case()))
    assert "keys are not configured" in result.settlement_detail


def test_stopped_outcomes_are_not_reconcilable():
    case = make_case()
    stopped = ActionOutcome(
        payment_id=case.payment_id, action=ActionType.STOP_NO_ACTION,
        status=ActionStatus.NO_ACTION, settlement_source=SettlementSource.NONE,
    )
    assert act.reconcile(stopped) == stopped
