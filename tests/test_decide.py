"""
Tests for the decide stage.

Three groups matter:

* **Precedence** — the eight rules are ordered, and the order *is* the safety
  property. A case must not be able to reach a customer-contacting rule while a
  gate above it also applies.
* **Coverage** — every recoverable cause has an action, and no cause anywhere in
  the taxonomy produces an unexplained decision.
* **The ratchet, again** — a diagnosis cannot widen permissions here either, even
  if app/diagnose.py were bypassed entirely.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.decide import (
    ACTION_MAP,
    contact_channel,
    cooldown_remaining,
    decide,
    decide_all,
    effective_recoverability,
    unmapped_recoverable_reasons,
)
from app.detect import normalize_row
from app.diagnose import rule_diagnosis
from app.models import (
    CUSTOMER_ACTION_REASONS,
    RECOVERABLE_REASONS,
    TRANSIENT_REASONS,
    UNRECOVERABLE_REASONS,
    ActionType,
    Diagnosis,
    DiagnosisSource,
    FailureReason,
    PolicyRule,
    Recoverability,
)

NOW = datetime(2026, 8, 25, 12, 0, 0, tzinfo=timezone.utc)
CAP = 3
COOLDOWN = 6


def make_case(**overrides):
    row = {
        "payment_id": "pay_dec_0001",
        "order_id": "order_dec_0001",
        "customer_id": "cust_dec_1",
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


def run(case, diagnosis=None):
    diagnosis = diagnosis or rule_diagnosis(case, DiagnosisSource.RULE_FALLBACK)
    return decide(case, diagnosis, now=NOW, max_retry_attempts=CAP, cooldown_hours=COOLDOWN)


# --------------------------------------------------------------------------
# Rule 1: the hard gate
# --------------------------------------------------------------------------

@pytest.mark.parametrize("reason", sorted(UNRECOVERABLE_REASONS, key=lambda r: r.value))
def test_every_gated_cause_stops(reason):
    decision = run(make_case(failure_reason=reason.value))
    assert decision.action is ActionType.STOP_NO_ACTION
    assert decision.policy_rule is PolicyRule.GATE_UNRECOVERABLE_CAUSE
    assert decision.contacts_customer is False


@pytest.mark.parametrize("reason", sorted(UNRECOVERABLE_REASONS, key=lambda r: r.value))
def test_gate_beats_every_other_condition(reason):
    """
    Precedence test. This case is simultaneously gated, uncontactable, over the
    retry cap and inside the cooldown window. The gate must win, because the
    reported reason for not acting has to be the most fundamental one.
    """
    case = make_case(
        failure_reason=reason.value,
        customer_email="junk",
        customer_phone="12345",
        retry_count="9",
        last_attempt_at=(NOW - timedelta(minutes=5)).isoformat(),
    )
    decision = run(case)
    assert decision.policy_rule is PolicyRule.GATE_UNRECOVERABLE_CAUSE
    assert decision.action is ActionType.STOP_NO_ACTION


def test_gated_decision_reports_the_money_it_declines():
    decision = run(make_case(failure_reason="fraud_suspected", amount_inr="2500.00"))
    assert "2500.00" in decision.reasoning
    assert "not chased" in decision.reasoning


# --------------------------------------------------------------------------
# Rule 2: unknown cause
# --------------------------------------------------------------------------

def test_unknown_cause_escalates_and_never_acts():
    decision = run(make_case(failure_reason="quantum_flux_declined"))
    assert decision.action is ActionType.ESCALATE_TO_HUMAN
    assert decision.policy_rule is PolicyRule.ESCALATE_UNKNOWN_CAUSE
    assert decision.contacts_customer is False
    assert "quantum_flux_declined" in decision.reasoning


def test_unknown_cause_beats_contactability_and_cooldown():
    case = make_case(
        failure_reason="mystery",
        retry_count="9",
        last_attempt_at=(NOW - timedelta(minutes=1)).isoformat(),
    )
    assert run(case).policy_rule is PolicyRule.ESCALATE_UNKNOWN_CAUSE


# --------------------------------------------------------------------------
# Rule 3: contactability
# --------------------------------------------------------------------------

def test_uncontactable_case_escalates_rather_than_pretending_to_act():
    case = make_case(customer_email="not-an-email", customer_phone="12345")
    decision = run(case)
    assert decision.action is ActionType.ESCALATE_TO_HUMAN
    assert decision.policy_rule is PolicyRule.ESCALATE_NOT_CONTACTABLE
    assert decision.contact_channel == "none"


@pytest.mark.parametrize(
    "email,phone,expected",
    [
        ("a@b.co", "9876543210", "email+sms"),
        ("a@b.co", "12345", "email"),
        ("junk", "9876543210", "sms"),
        ("junk", "12345", "none"),
    ],
)
def test_contact_channel_reflects_usable_channels(email, phone, expected):
    assert contact_channel(make_case(customer_email=email, customer_phone=phone)) == expected


def test_one_usable_channel_is_enough_to_act():
    case = make_case(customer_email="not-an-email", customer_phone="9876543210")
    assert run(case).action is ActionType.SEND_ALT_PAYMENT_LINK


# --------------------------------------------------------------------------
# Rule 4: retry cap
# --------------------------------------------------------------------------

@pytest.mark.parametrize("count", [3, 4, 17])
def test_retry_cap_escalates_instead_of_contacting_again(count):
    decision = run(make_case(retry_count=str(count)))
    assert decision.action is ActionType.ESCALATE_TO_HUMAN
    assert decision.policy_rule is PolicyRule.ESCALATE_RETRY_CAP_REACHED
    assert decision.contacts_customer is False
    assert f"{count} of {CAP}" in decision.reasoning


@pytest.mark.parametrize("count", [0, 1, 2])
def test_under_the_cap_the_agent_still_acts(count):
    decision = run(make_case(retry_count=str(count)))
    assert decision.contacts_customer is True
    assert f"Attempt {count + 1} of {CAP}" in decision.reasoning


def test_retry_cap_is_checked_before_cooldown():
    """
    The permanent stop should be reported ahead of the temporary one. A case at the
    cap is not going to be picked up by a later run, and reporting it as merely
    'in cooldown' would imply it will be.
    """
    case = make_case(retry_count="3", last_attempt_at=(NOW - timedelta(minutes=10)).isoformat())
    assert run(case).policy_rule is PolicyRule.ESCALATE_RETRY_CAP_REACHED


# --------------------------------------------------------------------------
# Rule 5: cooldown
# --------------------------------------------------------------------------

@pytest.mark.parametrize("hours_ago", [0.1, 1, 5.9])
def test_inside_the_cooldown_window_nothing_happens(hours_ago):
    case = make_case(
        retry_count="1", last_attempt_at=(NOW - timedelta(hours=hours_ago)).isoformat()
    )
    decision = run(case)
    assert decision.action is ActionType.STOP_NO_ACTION
    assert decision.policy_rule is PolicyRule.STOP_COOLDOWN_NOT_ELAPSED
    assert decision.contacts_customer is False


@pytest.mark.parametrize("hours_ago", [6, 6.1, 48])
def test_outside_the_cooldown_window_the_agent_acts(hours_ago):
    case = make_case(
        created_at=(NOW - timedelta(hours=hours_ago + 2)).isoformat(),
        retry_count="1",
        last_attempt_at=(NOW - timedelta(hours=hours_ago)).isoformat(),
    )
    assert run(case).contacts_customer is True


def test_cooldown_stop_is_framed_as_not_yet_rather_than_a_write_off():
    case = make_case(retry_count="1", last_attempt_at=(NOW - timedelta(hours=2)).isoformat())
    reasoning = run(case).reasoning
    assert "not yet" in reasoning
    assert "4.0h" in reasoning


def test_missing_last_attempt_does_not_strand_recoverable_money():
    """A clerical gap in the data must not permanently block a case."""
    case = make_case(retry_count="2", last_attempt_at="")
    assert cooldown_remaining(case, now=NOW, cooldown_hours=COOLDOWN) is None
    assert run(case).contacts_customer is True


def test_cooldown_remaining_is_none_once_elapsed():
    case = make_case(retry_count="1", last_attempt_at=(NOW - timedelta(hours=10)).isoformat())
    assert cooldown_remaining(case, now=NOW, cooldown_hours=COOLDOWN) is None


# --------------------------------------------------------------------------
# Rules 6 and 7: the action map
# --------------------------------------------------------------------------

@pytest.mark.parametrize("reason", sorted(TRANSIENT_REASONS, key=lambda r: r.value))
def test_transient_faults_retry_the_same_method(reason):
    decision = run(make_case(failure_reason=reason.value))
    assert decision.action is ActionType.RETRY_SAME_METHOD
    assert decision.policy_rule is PolicyRule.RETRY_TRANSIENT_FAULT
    assert "infrastructure fault" in decision.reasoning


@pytest.mark.parametrize("reason", sorted(CUSTOMER_ACTION_REASONS, key=lambda r: r.value))
def test_customer_side_faults_get_a_fresh_link(reason):
    decision = run(make_case(failure_reason=reason.value))
    assert decision.action is ActionType.SEND_ALT_PAYMENT_LINK
    assert decision.policy_rule is PolicyRule.LINK_CUSTOMER_ACTION_REQUIRED
    assert "would fail identically" in decision.reasoning


def test_every_recoverable_cause_has_an_action():
    """A new recoverable cause must not arrive without someone deciding what to
    do about it."""
    assert unmapped_recoverable_reasons() == set()
    assert set(ACTION_MAP) == set(RECOVERABLE_REASONS)


def test_the_action_map_never_maps_a_gated_cause():
    for reason in UNRECOVERABLE_REASONS:
        assert reason not in ACTION_MAP


def test_transient_and_customer_action_sets_are_disjoint():
    assert TRANSIENT_REASONS.isdisjoint(CUSTOMER_ACTION_REASONS)


# --------------------------------------------------------------------------
# The ratchet: a diagnosis cannot widen permissions
# --------------------------------------------------------------------------

def _forged(case, reason, recoverability):
    """A diagnosis that lies about recoverability, as if diagnose.py were bypassed."""
    return Diagnosis(
        payment_id=case.payment_id,
        reason=reason,
        recoverability=recoverability,
        source=DiagnosisSource.LLM,
        confidence=1.0,
        root_cause="forged",
    )


@pytest.mark.parametrize("reason", sorted(UNRECOVERABLE_REASONS, key=lambda r: r.value))
def test_a_diagnosis_cannot_unlock_a_gated_case(reason):
    case = make_case(failure_reason=reason.value)
    forged = _forged(case, FailureReason.BANK_TIMEOUT, Recoverability.RECOVERABLE)
    decision = decide(case, forged, now=NOW, max_retry_attempts=CAP, cooldown_hours=COOLDOWN)
    assert decision.action is ActionType.STOP_NO_ACTION
    assert decision.policy_rule is PolicyRule.GATE_UNRECOVERABLE_CAUSE


def test_a_diagnosis_cannot_promote_an_unknown_cause():
    case = make_case(failure_reason="totally_unmapped")
    forged = _forged(case, FailureReason.CARD_EXPIRED, Recoverability.RECOVERABLE)
    decision = decide(case, forged, now=NOW, max_retry_attempts=CAP, cooldown_hours=COOLDOWN)
    assert decision.action is ActionType.ESCALATE_TO_HUMAN
    assert decision.policy_rule is PolicyRule.ESCALATE_UNKNOWN_CAUSE


def test_a_diagnosis_can_still_narrow():
    case = make_case(failure_reason="bank_timeout")
    narrowed = _forged(case, FailureReason.FRAUD_SUSPECTED, Recoverability.UNRECOVERABLE)
    decision = decide(case, narrowed, now=NOW, max_retry_attempts=CAP, cooldown_hours=COOLDOWN)
    assert decision.action is ActionType.STOP_NO_ACTION


@pytest.mark.parametrize(
    "case_r,diag_r,expected",
    [
        (Recoverability.RECOVERABLE, Recoverability.RECOVERABLE, Recoverability.RECOVERABLE),
        (Recoverability.RECOVERABLE, Recoverability.UNKNOWN, Recoverability.UNKNOWN),
        (Recoverability.RECOVERABLE, Recoverability.UNRECOVERABLE, Recoverability.UNRECOVERABLE),
        (Recoverability.UNKNOWN, Recoverability.RECOVERABLE, Recoverability.UNKNOWN),
        (Recoverability.UNRECOVERABLE, Recoverability.RECOVERABLE, Recoverability.UNRECOVERABLE),
        (Recoverability.UNKNOWN, Recoverability.UNRECOVERABLE, Recoverability.UNRECOVERABLE),
    ],
)
def test_effective_recoverability_always_takes_the_stricter_reading(case_r, diag_r, expected):
    case = make_case()
    case = case.model_copy(update={"recoverability": case_r})
    diagnosis = _forged(case, FailureReason.BANK_TIMEOUT, diag_r)
    assert effective_recoverability(case, diagnosis) is expected


# --------------------------------------------------------------------------
# LLM suggestion accounting
# --------------------------------------------------------------------------

def test_suggestion_is_recorded_as_honoured_when_the_map_agrees():
    case = make_case(failure_reason="card_expired")
    diagnosis = rule_diagnosis(case, DiagnosisSource.LLM).model_copy(
        update={"suggested_action": ActionType.SEND_ALT_PAYMENT_LINK}
    )
    assert run(case, diagnosis).llm_suggestion_honoured is True


def test_suggestion_is_recorded_as_overruled_when_the_map_disagrees():
    case = make_case(failure_reason="card_expired")
    diagnosis = rule_diagnosis(case, DiagnosisSource.LLM).model_copy(
        update={"suggested_action": ActionType.RETRY_SAME_METHOD}
    )
    decision = run(case, diagnosis)
    assert decision.action is ActionType.SEND_ALT_PAYMENT_LINK
    assert decision.llm_suggestion_honoured is False


def test_no_suggestion_accounting_when_the_llm_was_never_consulted():
    case = make_case(failure_reason="card_expired")
    assert run(case).llm_suggestion_honoured is None


# --------------------------------------------------------------------------
# Exhaustive invariants
# --------------------------------------------------------------------------

@pytest.mark.parametrize("reason", sorted(FailureReason, key=lambda r: r.value))
@pytest.mark.parametrize("retry_count", ["0", "2", "3"])
@pytest.mark.parametrize("contactable", [True, False])
def test_every_combination_yields_exactly_one_explained_decision(reason, retry_count, contactable):
    case = make_case(
        failure_reason=reason.value,
        retry_count=retry_count,
        customer_email="a@b.co" if contactable else "junk",
        customer_phone="9876543210" if contactable else "1",
    )
    decision = run(case)
    assert isinstance(decision.action, ActionType)
    assert isinstance(decision.policy_rule, PolicyRule)
    assert decision.reasoning.strip(), "a decision with no explanation is not auditable"
    assert decision.amount_paise == case.amount_paise
    # No case may be contacted without a usable channel.
    if decision.contacts_customer:
        assert case.is_contactable
        assert case.recoverability is Recoverability.RECOVERABLE
        assert int(retry_count) < CAP


def test_no_gated_cause_can_ever_produce_a_contacting_action():
    for reason in UNRECOVERABLE_REASONS:
        for retry_count in ["0", "1", "2", "3"]:
            decision = run(make_case(failure_reason=reason.value, retry_count=retry_count))
            assert decision.contacts_customer is False


def test_decide_all_refuses_a_length_mismatch():
    cases = [make_case(payment_id="pay_1"), make_case(payment_id="pay_2")]
    with pytest.raises(ValueError, match="length mismatch"):
        decide_all(cases, [rule_diagnosis(cases[0], DiagnosisSource.RULE_FALLBACK)], now=NOW)


def test_decide_all_is_index_aligned():
    cases = [make_case(payment_id=f"pay_{i}") for i in range(4)]
    diagnoses = [rule_diagnosis(c, DiagnosisSource.RULE_FALLBACK) for c in cases]
    decisions = decide_all(cases, diagnoses, now=NOW)
    assert [d.payment_id for d in decisions] == [c.payment_id for c in cases]
