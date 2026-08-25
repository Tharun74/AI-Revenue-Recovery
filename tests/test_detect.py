"""
Tests for the detect stage.

The important ones here are the gate tests: `test_every_unrecoverable_cause_is_classified_unrecoverable`
and `test_no_recoverable_cause_leaks_into_the_unrecoverable_set`. If the
recoverable/unrecoverable split is wrong, the agent will either harass customers
it should leave alone or abandon money it should chase.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.detect import (
    build_summary,
    detect,
    map_failure_reason,
    normalize_email,
    normalize_phone,
    normalize_row,
    parse_amount_to_paise,
    parse_timestamp,
)
from app.models import (
    RECOVERABLE_REASONS,
    UNRECOVERABLE_REASONS,
    FailureReason,
    LeakType,
    Recoverability,
    RejectReason,
    classify_recoverability,
)

NOW = datetime(2026, 8, 25, 12, 0, 0, tzinfo=timezone.utc)


def make_row(**overrides) -> dict:
    row = {
        "payment_id": "pay_test000000001",
        "order_id": "order_test00000001",
        "customer_id": "cust_test001",
        "customer_name": "Aarav Sharma",
        "customer_email": "aarav.sharma@example.com",
        "customer_phone": "9876543210",
        "amount_inr": "1499.50",
        "currency": "INR",
        "failure_reason": "insufficient_funds",
        "error_description": "Insufficient balance",
        "created_at": (NOW - timedelta(hours=5)).isoformat(),
        "retry_count": "0",
        "last_attempt_at": "",
    }
    row.update(overrides)
    return row


def one(**overrides):
    """Normalize a single row and return (case, rejection)."""
    return normalize_row(1, make_row(**overrides), set(), now=NOW)


# --------------------------------------------------------------------------
# The gate: recoverable vs unrecoverable
# --------------------------------------------------------------------------

@pytest.mark.parametrize("reason", sorted(UNRECOVERABLE_REASONS, key=lambda r: r.value))
def test_every_unrecoverable_cause_is_classified_unrecoverable(reason):
    assert classify_recoverability(reason) is Recoverability.UNRECOVERABLE


@pytest.mark.parametrize("reason", sorted(RECOVERABLE_REASONS, key=lambda r: r.value))
def test_every_recoverable_cause_is_classified_recoverable(reason):
    assert classify_recoverability(reason) is Recoverability.RECOVERABLE


def test_no_recoverable_cause_leaks_into_the_unrecoverable_set():
    assert RECOVERABLE_REASONS.isdisjoint(UNRECOVERABLE_REASONS)


def test_every_known_reason_is_classified():
    """No cause may fall through the taxonomy unnoticed."""
    for reason in FailureReason:
        if reason is FailureReason.UNKNOWN:
            assert classify_recoverability(reason) is Recoverability.UNKNOWN
        else:
            assert classify_recoverability(reason) is not Recoverability.UNKNOWN, (
                f"{reason.value} is in neither RECOVERABLE_REASONS nor UNRECOVERABLE_REASONS"
            )


def test_unknown_cause_is_not_silently_treated_as_recoverable():
    case, rejection = one(failure_reason="quantum_flux_declined")
    assert rejection is None
    assert case.failure_reason is FailureReason.UNKNOWN
    assert case.recoverability is Recoverability.UNKNOWN
    assert case.raw_failure_reason == "quantum_flux_declined"
    assert any("not in the known taxonomy" in w for w in case.data_warnings)


# --------------------------------------------------------------------------
# Money: integer paise, exact
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1499.50", 149950),
        ("1,499.50", 149950),
        ("₹999", 99900),
        ("299", 29900),
        (" 0.01 ", 1),
        ("1003.21", 100321),
    ],
)
def test_amount_parses_to_exact_paise(raw, expected):
    assert parse_amount_to_paise(raw) == expected


@pytest.mark.parametrize("raw", ["-1499.00", "0", "0.00", "N/A", "", None, "abc", "nan"])
def test_untrustworthy_amounts_are_refused(raw):
    assert parse_amount_to_paise(raw) is None


def test_amount_totals_stay_exact_across_a_batch():
    """Float accumulation would drift here; integer paise must not."""
    rows = [make_row(payment_id=f"pay_{i:04d}", amount_inr="0.10") for i in range(1000)]
    batch = detect(rows=rows, now=NOW)
    assert batch.summary.total_at_risk_paise == 10_000
    assert batch.summary.total_at_risk_inr == 100.00


# --------------------------------------------------------------------------
# Rejections: a bad row never kills the batch
# --------------------------------------------------------------------------

def test_missing_payment_id_is_rejected():
    case, rejection = one(payment_id="")
    assert case is None
    assert rejection.reject_reason is RejectReason.MISSING_PAYMENT_ID


def test_negative_amount_is_rejected():
    case, rejection = one(amount_inr="-500.00")
    assert case is None
    assert rejection.reject_reason is RejectReason.INVALID_AMOUNT


def test_unparseable_timestamp_is_rejected():
    case, rejection = one(created_at="not-a-date")
    assert case is None
    assert rejection.reject_reason is RejectReason.INVALID_TIMESTAMP


def test_duplicate_payment_id_is_rejected_once_not_twice():
    rows = [make_row(), make_row(amount_inr="777.00")]
    batch = detect(rows=rows, now=NOW)
    assert len(batch.cases) == 1
    assert len(batch.rejected) == 1
    assert batch.rejected[0].reject_reason is RejectReason.DUPLICATE_PAYMENT_ID
    # the first occurrence is the one kept
    assert batch.cases[0].amount_paise == 149950


def test_rejected_row_preserves_raw_input_for_audit():
    case, rejection = one(payment_id="", customer_name="Meera Iyer")
    assert rejection.raw["customer_name"] == "Meera Iyer"
    assert rejection.row_number == 1
    assert rejection.detail


def test_one_bad_row_does_not_kill_the_batch():
    rows = [
        make_row(payment_id="pay_ok_1"),
        make_row(payment_id="", customer_name="Broken"),
        make_row(payment_id="pay_ok_2"),
        make_row(payment_id="pay_bad_amt", amount_inr="oops"),
        make_row(payment_id="pay_ok_3"),
    ]
    batch = detect(rows=rows, now=NOW)
    assert batch.rows_read == 5
    assert [c.payment_id for c in batch.cases] == ["pay_ok_1", "pay_ok_2", "pay_ok_3"]
    assert batch.summary.rejected_rows == 2
    assert batch.summary.by_reject_reason == {
        "missing_payment_id": 1,
        "invalid_amount": 1,
    }


def test_every_row_is_accounted_for():
    """cases + rejected must equal rows_read — money cannot silently vanish."""
    rows = [
        make_row(payment_id="pay_a"),
        make_row(payment_id=""),
        make_row(payment_id="pay_b", amount_inr="-1"),
        make_row(payment_id="pay_c", created_at="junk"),
        make_row(payment_id="pay_a"),
        make_row(payment_id="pay_d", failure_reason="totally_unknown"),
    ]
    batch = detect(rows=rows, now=NOW)
    assert len(batch.cases) + len(batch.rejected) == batch.rows_read == 6


# --------------------------------------------------------------------------
# Timestamps: tz-aware, and never in the future
# --------------------------------------------------------------------------

def test_naive_timestamps_are_assumed_utc_and_made_aware():
    parsed = parse_timestamp("2026-08-22T23:09:18.604561")
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)


def test_z_suffix_timestamps_parse():
    assert parse_timestamp("2026-08-22T23:09:18Z") == datetime(
        2026, 8, 22, 23, 9, 18, tzinfo=timezone.utc
    )


def test_cooldown_arithmetic_never_raises_on_mixed_awareness():
    """The whole point of normalizing at the edge: this subtraction must work."""
    case, _ = one(
        retry_count="2",
        last_attempt_at=(NOW - timedelta(hours=3)).isoformat(),
    )
    elapsed = NOW - case.last_attempt_at
    assert elapsed == timedelta(hours=3)


def test_future_last_attempt_is_clamped_not_trusted():
    case, _ = one(retry_count="1", last_attempt_at=(NOW + timedelta(hours=19)).isoformat())
    assert case.last_attempt_at == NOW
    assert any("future" in w for w in case.data_warnings)


def test_last_attempt_before_created_is_discarded():
    case, _ = one(
        created_at=(NOW - timedelta(hours=2)).isoformat(),
        retry_count="1",
        last_attempt_at=(NOW - timedelta(hours=10)).isoformat(),
    )
    assert case.last_attempt_at is None
    assert any("precedes created_at" in w for w in case.data_warnings)


def test_future_created_at_is_clamped():
    case, _ = one(created_at=(NOW + timedelta(days=2)).isoformat())
    assert case.created_at == NOW
    assert any("future" in w for w in case.data_warnings)


# --------------------------------------------------------------------------
# Contactability
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("9876543210", "+919876543210"),
        ("+91 98765 43210", "+919876543210"),
        ("919876543210", "+919876543210"),
        ("09876543210", "+919876543210"),
        ("98765-43210", "+919876543210"),
    ],
)
def test_usable_phones_normalize_to_e164(raw, expected):
    normalized, ok = normalize_phone(raw)
    assert ok is True
    assert normalized == expected


@pytest.mark.parametrize("raw", ["12345", "1234567890", "", None, "abc", "5876543210"])
def test_unusable_phones_are_flagged(raw):
    _, ok = normalize_phone(raw)
    assert ok is False


@pytest.mark.parametrize(
    "raw,ok",
    [
        ("Aarav.Sharma@Example.com", True),
        ("a@b.co", True),
        ("not-an-email", False),
        ("a@b", False),
        ("", False),
        (None, False),
    ],
)
def test_email_usability(raw, ok):
    _, usable = normalize_email(raw)
    assert usable is ok


def test_email_is_lowercased():
    normalized, _ = normalize_email("  Aarav.SHARMA@Example.COM ")
    assert normalized == "aarav.sharma@example.com"


def test_case_with_no_channel_is_not_contactable():
    case, _ = one(customer_email="not-an-email", customer_phone="12345")
    assert case.is_contactable is False
    assert any("not contactable" in w for w in case.data_warnings)


def test_one_good_channel_is_enough():
    case, _ = one(customer_email="not-an-email", customer_phone="9876543210")
    assert case.is_contactable is True


# --------------------------------------------------------------------------
# Reason alias mapping
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("insufficient_funds", FailureReason.INSUFFICIENT_FUNDS),
        ("Insufficient Balance", FailureReason.INSUFFICIENT_FUNDS),
        ("INSUFFICIENT-FUNDS", FailureReason.INSUFFICIENT_FUNDS),
        ("  bank_timeout  ", FailureReason.BANK_TIMEOUT),
        ("suspected_fraud", FailureReason.FRAUD_SUSPECTED),
        ("user_cancelled", FailureReason.CUSTOMER_CANCELLED),
        ("blocked_card", FailureReason.CARD_BLOCKED),
        ("who_knows", FailureReason.UNKNOWN),
        ("", FailureReason.UNKNOWN),
        (None, FailureReason.UNKNOWN),
    ],
)
def test_reason_aliases_map_correctly(raw, expected):
    assert map_failure_reason(raw) is expected


def test_fraud_aliases_still_hit_the_gate():
    """An aliased fraud reason must be just as blocked as the canonical spelling."""
    for alias in ["fraud", "suspected_fraud", "risk_blocked", "FRAUD_SUSPECTED"]:
        reason = map_failure_reason(alias)
        assert reason is FailureReason.FRAUD_SUSPECTED
        assert classify_recoverability(reason) is Recoverability.UNRECOVERABLE


# --------------------------------------------------------------------------
# Summary aggregation
# --------------------------------------------------------------------------

def test_summary_splits_money_by_recoverability():
    rows = [
        make_row(payment_id="pay_1", amount_inr="100.00", failure_reason="insufficient_funds"),
        make_row(payment_id="pay_2", amount_inr="200.00", failure_reason="bank_timeout"),
        make_row(payment_id="pay_3", amount_inr="300.00", failure_reason="fraud_suspected"),
        make_row(payment_id="pay_4", amount_inr="400.00", failure_reason="customer_cancelled"),
        make_row(payment_id="pay_5", amount_inr="500.00", failure_reason="mystery_code"),
    ]
    s = detect(rows=rows, now=NOW).summary

    assert s.total_cases == 5
    assert s.total_at_risk_inr == 1500.00
    assert (s.recoverable_cases, s.recoverable_inr) == (2, 300.00)
    assert (s.unrecoverable_cases, s.unrecoverable_inr) == (2, 700.00)
    assert (s.unknown_cases, s.unknown_inr) == (1, 500.00)
    # every case lands in exactly one bucket
    assert s.recoverable_cases + s.unrecoverable_cases + s.unknown_cases == s.total_cases
    assert (
        s.recoverable_paise + s.unrecoverable_paise + s.unknown_paise
        == s.total_at_risk_paise
    )


def test_summary_by_reason_totals_reconcile():
    rows = [
        make_row(payment_id="pay_1", amount_inr="100.00", failure_reason="insufficient_funds"),
        make_row(payment_id="pay_2", amount_inr="250.00", failure_reason="insufficient_funds"),
        make_row(payment_id="pay_3", amount_inr="300.00", failure_reason="card_expired"),
    ]
    s = detect(rows=rows, now=NOW).summary
    assert s.by_reason["insufficient_funds"].count == 2
    assert s.by_reason["insufficient_funds"].amount_inr == 350.00
    assert sum(b.amount_paise for b in s.by_reason.values()) == s.total_at_risk_paise
    assert sum(b.count for b in s.by_reason.values()) == s.total_cases


def test_summary_counts_retry_cap_cases():
    rows = [
        make_row(payment_id="pay_1", retry_count="0"),
        make_row(payment_id="pay_2", retry_count="3"),
        make_row(payment_id="pay_3", retry_count="5"),
    ]
    cases = detect(rows=rows, now=NOW).cases
    s = build_summary(cases, [], max_retry_attempts=3)
    assert s.at_retry_cap_cases == 2


def test_summary_counts_uncontactable_cases():
    rows = [
        make_row(payment_id="pay_1"),
        make_row(payment_id="pay_2", customer_email="junk", customer_phone="1"),
    ]
    s = detect(rows=rows, now=NOW).summary
    assert s.not_contactable_cases == 1


def test_empty_batch_produces_a_valid_empty_summary():
    batch = detect(rows=[], now=NOW)
    assert batch.rows_read == 0
    assert batch.cases == []
    assert batch.summary.total_cases == 0
    assert batch.summary.total_at_risk_inr == 0.0


# --------------------------------------------------------------------------
# Misc invariants
# --------------------------------------------------------------------------

def test_leak_type_is_tagged_on_every_case():
    batch = detect(rows=[make_row()], now=NOW)
    assert batch.cases[0].leak_type is LeakType.FAILED_PAYMENT


def test_negative_retry_count_is_defaulted_to_zero():
    case, _ = one(retry_count="-3")
    assert case.retry_count == 0
    assert any("negative" in w for w in case.data_warnings)


def test_non_inr_currency_is_flagged_not_silently_converted():
    case, _ = one(currency="USD")
    assert case.currency == "USD"
    assert any("not INR" in w for w in case.data_warnings)


def test_clean_row_carries_no_warnings():
    case, rejection = one()
    assert rejection is None
    assert case.data_warnings == []
    assert case.is_contactable is True
    assert case.recoverability is Recoverability.RECOVERABLE
