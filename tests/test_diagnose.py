"""
Tests for the diagnose stage.

The load-bearing tests here are the permission tests:
`test_unrecoverable_case_never_reaches_the_llm`,
`test_llm_may_not_relabel_an_unknown_cause_as_actionable` and
`test_llm_may_narrow_a_recoverable_case_to_unrecoverable`. Together they assert the
one-way ratchet the module exists to enforce — a model can take permissions away,
never hand them out.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from app import diagnose as diagnose_stage
from app.detect import normalize_row
from app.diagnose import (
    DiagnoseSession,
    _extract_json,
    build_user_prompt,
    diagnose,
    diagnose_all,
    rule_diagnosis,
    signature,
)
from app.models import (
    UNRECOVERABLE_REASONS,
    ActionType,
    DiagnosisSource,
    FailureReason,
    Recoverability,
)
from app.services import llm_client

NOW = datetime(2026, 8, 25, 12, 0, 0, tzinfo=timezone.utc)


def make_case(**overrides):
    row = {
        "payment_id": "pay_diag_0001",
        "order_id": "order_diag_0001",
        "customer_id": "cust_diag_1",
        "customer_name": "Aarav Sharma",
        "customer_email": "aarav@example.com",
        "customer_phone": "9876543210",
        "amount_inr": "1499.00",
        "currency": "INR",
        "failure_reason": "insufficient_funds",
        "error_description": "Insufficient balance",
        "created_at": (NOW - timedelta(hours=5)).isoformat(),
        "retry_count": "0",
        "last_attempt_at": "",
    }
    row.update(overrides)
    case, rejection = normalize_row(1, row, set(), now=NOW)
    assert rejection is None, rejection
    return case


@pytest.fixture
def stub_llm(monkeypatch):
    """
    Replace the transport, not the logic. `complete` is the single seam between
    this module and Anthropic, so stubbing it exercises every line of prompt
    building, parsing and boundary enforcement for real.
    """
    calls: list[tuple[str, str]] = []

    def install(reply: str = "", error: str = ""):
        def fake_complete(system, user, max_tokens=None):
            calls.append((system, user))
            return reply, error

        monkeypatch.setattr(llm_client, "is_available", lambda: True)
        monkeypatch.setattr(llm_client, "unavailable_reason", lambda: "")
        monkeypatch.setattr(llm_client, "complete", fake_complete)
        return calls

    return install


def reply(**fields) -> str:
    payload = {
        "cause": "insufficient_funds",
        "likely_transient": False,
        "confidence": 0.9,
        "root_cause": "Balance too low at the time of the charge.",
        "recommended_action": "send_alt_payment_link",
    }
    payload.update(fields)
    return json.dumps(payload)


# --------------------------------------------------------------------------
# The gate: an unrecoverable case is never sent to the model
# --------------------------------------------------------------------------

@pytest.mark.parametrize("reason", sorted(UNRECOVERABLE_REASONS, key=lambda r: r.value))
def test_unrecoverable_case_never_reaches_the_llm(reason, stub_llm):
    calls = stub_llm(reply=reply(cause="insufficient_funds"))
    case = make_case(failure_reason=reason.value)

    result = diagnose(case, use_llm=True)

    assert result.source is DiagnosisSource.GATE
    assert result.recoverability is Recoverability.UNRECOVERABLE
    assert result.reason is reason
    assert result.suggested_action is None
    assert calls == [], "the model was consulted about a case the agent may not touch"


def test_gated_diagnosis_still_explains_itself():
    case = make_case(failure_reason="fraud_suspected")
    result = diagnose(case, use_llm=True)
    assert result.root_cause
    assert "compliance" in result.root_cause.lower()


# --------------------------------------------------------------------------
# The one-way ratchet
# --------------------------------------------------------------------------

def test_llm_may_narrow_a_recoverable_case_to_unrecoverable(stub_llm):
    """Narrowing is the safe direction, so it is honoured."""
    stub_llm(reply=reply(cause="fraud_suspected", root_cause="Velocity pattern looks like card testing."))
    case = make_case(failure_reason="gateway_error")

    result = diagnose(case, use_llm=True)

    assert result.reason is FailureReason.FRAUD_SUSPECTED
    assert result.recoverability is Recoverability.UNRECOVERABLE
    assert any("downgraded" in v for v in result.boundary_violations)


def test_llm_may_not_relabel_an_unknown_cause_as_actionable(stub_llm):
    """
    The one that matters most. An unmapped reason string must not become an
    actionable case because a model recognised it.
    """
    stub_llm(reply=reply(cause="bank_timeout", confidence=0.95))
    case = make_case(failure_reason="quantum_flux_declined")
    assert case.recoverability is Recoverability.UNKNOWN

    result = diagnose(case, use_llm=True)

    assert result.reason is FailureReason.UNKNOWN
    assert result.recoverability is Recoverability.UNKNOWN
    assert any("refused to relabel" in v for v in result.boundary_violations)
    # The reading survives as context for the human who picks this up.
    assert result.llm_raw


def test_llm_may_narrow_an_unknown_cause_to_unrecoverable(stub_llm):
    """The safe direction is allowed even from UNKNOWN."""
    stub_llm(reply=reply(cause="customer_cancelled"))
    case = make_case(failure_reason="who_knows_what_this_is")

    result = diagnose(case, use_llm=True)

    assert result.reason is FailureReason.CUSTOMER_CANCELLED
    assert result.recoverability is Recoverability.UNRECOVERABLE


def test_recoverable_disagreement_keeps_the_gateway_reason(stub_llm):
    stub_llm(reply=reply(cause="card_expired"))
    case = make_case(failure_reason="bank_timeout")

    result = diagnose(case, use_llm=True)

    assert result.reason is FailureReason.BANK_TIMEOUT
    assert any("disagreement" in v for v in result.boundary_violations)


def test_cause_outside_the_taxonomy_is_discarded(stub_llm):
    stub_llm(reply=reply(cause="the_vibes_were_off"))
    case = make_case(failure_reason="bank_timeout")

    result = diagnose(case, use_llm=True)

    assert result.reason is FailureReason.BANK_TIMEOUT
    assert any("outside the taxonomy" in v for v in result.boundary_violations)


def test_invented_action_is_refused(stub_llm):
    stub_llm(reply=reply(recommended_action="charge_the_card_twice"))
    case = make_case(failure_reason="insufficient_funds")

    result = diagnose(case, use_llm=True)

    assert result.suggested_action is None
    assert any("not a permitted action" in v for v in result.boundary_violations)


def test_suggested_action_on_a_narrowed_case_is_dropped(stub_llm):
    """If the model both narrows the cause and suggests contacting the customer,
    the suggestion must not survive the narrowing."""
    stub_llm(reply=reply(cause="card_blocked", recommended_action="send_alt_payment_link"))
    case = make_case(failure_reason="bank_timeout")

    result = diagnose(case, use_llm=True)

    assert result.recoverability is Recoverability.UNRECOVERABLE
    assert result.suggested_action is None
    assert any("discarded" in v for v in result.boundary_violations)


def test_advisory_suggestion_is_kept_on_a_recoverable_case(stub_llm):
    stub_llm(reply=reply(cause="card_expired", recommended_action="send_alt_payment_link"))
    case = make_case(failure_reason="card_expired")

    result = diagnose(case, use_llm=True)

    assert result.suggested_action is ActionType.SEND_ALT_PAYMENT_LINK
    assert result.boundary_violations == []


# --------------------------------------------------------------------------
# Degradation: no key, bad key, garbage reply
# --------------------------------------------------------------------------

def test_missing_key_degrades_to_rules_and_says_so(monkeypatch):
    monkeypatch.setattr(llm_client, "is_available", lambda: False)
    monkeypatch.setattr(llm_client, "unavailable_reason", lambda: "FIREWORKS_API_KEY is not set")

    result = diagnose(make_case(), use_llm=True)

    assert result.source is DiagnosisSource.RULE_FALLBACK
    assert "FIREWORKS_API_KEY" in result.root_cause


def test_use_llm_false_skips_the_model_entirely(stub_llm):
    calls = stub_llm(reply=reply())
    result = diagnose(make_case(), use_llm=False)
    assert result.source is DiagnosisSource.RULE_FALLBACK
    assert calls == []


def test_api_error_degrades_to_rules(stub_llm):
    stub_llm(error="AuthenticationError: 401")
    result = diagnose(make_case(), use_llm=True)
    assert result.source is DiagnosisSource.RULE_FALLBACK
    assert "401" in result.root_cause


def test_unparseable_reply_degrades_but_keeps_the_raw_text(stub_llm):
    stub_llm(reply="I'd rather write you a poem about payments.")
    result = diagnose(make_case(), use_llm=True)
    assert result.source is DiagnosisSource.RULE_FALLBACK
    assert "poem" in result.llm_raw


def test_rule_fallback_still_classifies_every_known_cause():
    for reason in FailureReason:
        case = make_case(failure_reason=reason.value)
        result = diagnose(case, use_llm=False)
        assert result.root_cause, f"{reason.value} has no narration"
        assert result.reason is reason


# --------------------------------------------------------------------------
# Circuit breaker and cache
# --------------------------------------------------------------------------

def test_circuit_breaker_stops_calling_after_repeated_failures(stub_llm):
    calls = stub_llm(error="AuthenticationError: 401")
    session = DiagnoseSession(use_llm=True, failure_budget=2)
    cases = [make_case(payment_id=f"pay_{i}") for i in range(10)]

    results = [diagnose(c, use_llm=True, session=session) for c in cases]

    assert len(calls) == 2, "a bad key must not be retried once per case"
    assert session.tripped_reason
    assert all(r.source is DiagnosisSource.RULE_FALLBACK for r in results)
    assert "disabled after 2 consecutive failures" in results[-1].root_cause


def test_identical_signatures_are_only_diagnosed_once(stub_llm):
    calls = stub_llm(reply=reply())
    session = DiagnoseSession(use_llm=True)
    cases = [make_case(payment_id=f"pay_{i}") for i in range(6)]

    results = [diagnose(c, use_llm=True, session=session) for c in cases]

    assert len(calls) == 1
    assert results[0].source is DiagnosisSource.LLM
    assert all(r.source is DiagnosisSource.LLM_CACHE for r in results[1:])
    # A cached diagnosis must still be about the case it was applied to.
    assert [r.payment_id for r in results] == [c.payment_id for c in cases]


def test_different_error_text_is_not_shared_by_the_cache(stub_llm):
    calls = stub_llm(reply=reply())
    session = DiagnoseSession(use_llm=True)
    diagnose(make_case(payment_id="pay_a", error_description="one"), use_llm=True, session=session)
    diagnose(make_case(payment_id="pay_b", error_description="two"), use_llm=True, session=session)
    assert len(calls) == 2


def test_signature_ignores_money_and_customer():
    a = make_case(payment_id="pay_a", amount_inr="100.00", customer_name="A", retry_count="0")
    b = make_case(payment_id="pay_b", amount_inr="9999.00", customer_name="B", retry_count="2")
    assert signature(a) == signature(b)


# --------------------------------------------------------------------------
# Prompt hygiene
# --------------------------------------------------------------------------

def test_prompt_carries_no_customer_pii():
    case = make_case(
        customer_name="Meera Iyer",
        customer_email="meera.iyer@example.com",
        customer_phone="9812345678",
    )
    prompt = build_user_prompt(case)
    assert "Meera" not in prompt
    assert "meera.iyer@example.com" not in prompt
    assert "9812345678" not in prompt
    # It does carry the evidence needed to classify.
    assert "insufficient_funds" in prompt
    assert "reachable_by_email: True" in prompt


def test_system_prompt_pins_the_allowed_vocabulary():
    for reason in FailureReason:
        assert reason.value in diagnose_stage.SYSTEM_PROMPT
    for action in ActionType:
        assert action.value in diagnose_stage.SYSTEM_PROMPT


# --------------------------------------------------------------------------
# Reply parsing
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text",
    [
        '{"cause": "bank_timeout"}',
        '```json\n{"cause": "bank_timeout"}\n```',
        '```\n{"cause": "bank_timeout"}\n```',
        'Sure! Here you go:\n{"cause": "bank_timeout"}\nHope that helps.',
    ],
)
def test_json_survives_fences_and_chatter(text):
    assert _extract_json(text) == {"cause": "bank_timeout"}


@pytest.mark.parametrize("text", ["", "not json", "[1,2,3]", "{oops"])
def test_unparseable_text_returns_none(text):
    assert _extract_json(text) is None


def test_confidence_is_clamped_to_the_unit_interval(stub_llm):
    stub_llm(reply=reply(confidence=42))
    assert diagnose(make_case(), use_llm=True).confidence == 1.0
    stub_llm(reply=reply(confidence=-3))
    assert diagnose(make_case(), use_llm=True).confidence == 0.0
    stub_llm(reply=reply(confidence="not a number"))
    assert diagnose(make_case(), use_llm=True).confidence == 0.0


# --------------------------------------------------------------------------
# Batch behaviour
# --------------------------------------------------------------------------

def test_diagnose_all_returns_one_diagnosis_per_case(stub_llm):
    stub_llm(reply=reply())
    cases = [make_case(payment_id=f"pay_{i}") for i in range(5)]
    results = diagnose_all(cases, use_llm=True)
    assert len(results) == len(cases)
    assert [r.payment_id for r in results] == [c.payment_id for c in cases]


def test_rule_diagnosis_never_suggests_an_action():
    for reason in FailureReason:
        case = make_case(failure_reason=reason.value)
        assert rule_diagnosis(case, DiagnosisSource.RULE_FALLBACK).suggested_action is None
