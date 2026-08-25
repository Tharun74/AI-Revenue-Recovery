"""
Detect stage: turn a raw batch of records into validated, normalized at-risk
cases — plus an explicit list of what could not be read and why.

Two principles drive this module:

1. **A bad row never kills the batch.** Real recovery data is dirty. Each row is
   normalized and validated independently; failures are collected as
   `RejectedRecord`s and reported. Silently dropping a row would mean money
   quietly vanishing from the metrics, which defeats the point of the agent.

2. **Normalize once, at the edge.** Downstream stages (diagnose/decide/act) can
   then assume: money is integer paise, timestamps are tz-aware UTC, phones are
   E.164, and the recoverable/unrecoverable split is already decided.
"""

from __future__ import annotations

import csv
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from app.config import settings
from app.models import (
    DetectionBatch,
    DetectionSummary,
    FailedPayment,
    FailureReason,
    LeakType,
    ReasonBreakdown,
    RejectReason,
    RejectedRecord,
    classify_recoverability,
    utcnow,
)

DEFAULT_DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "failed_payments.csv"

# Deliberately permissive: we only need to know whether the address is usable as
# a contact channel, not whether it's RFC-5322 perfect.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]{2,}$")

# Indian mobile numbers: 10 digits starting 6-9, optionally +91 / 91 / 0 prefixed.
_PHONE_DIGITS_RE = re.compile(r"\D+")

#: Input strings we accept as aliases for a known cause. Anything not in here
#: becomes FailureReason.UNKNOWN and gets escalated to a human rather than
#: guessed at.
_REASON_ALIASES: dict[str, FailureReason] = {
    # canonical
    **{r.value: r for r in FailureReason if r is not FailureReason.UNKNOWN},
    # common real-world variants / Razorpay-ish phrasings
    "insufficient_balance": FailureReason.INSUFFICIENT_FUNDS,
    "insufficient funds": FailureReason.INSUFFICIENT_FUNDS,
    "low_balance": FailureReason.INSUFFICIENT_FUNDS,
    "bank_timed_out": FailureReason.BANK_TIMEOUT,
    "timeout": FailureReason.BANK_TIMEOUT,
    "gateway_timeout": FailureReason.GATEWAY_ERROR,
    "payment_gateway_error": FailureReason.GATEWAY_ERROR,
    "otp_incorrect": FailureReason.INVALID_OTP,
    "invalid_otp_entered": FailureReason.INVALID_OTP,
    "expired_card": FailureReason.CARD_EXPIRED,
    "card_has_expired": FailureReason.CARD_EXPIRED,
    "network_failure": FailureReason.NETWORK_ERROR,
    "issuer_down": FailureReason.ISSUER_UNAVAILABLE,
    "bank_unavailable": FailureReason.ISSUER_UNAVAILABLE,
    "fraud": FailureReason.FRAUD_SUSPECTED,
    "suspected_fraud": FailureReason.FRAUD_SUSPECTED,
    "risk_blocked": FailureReason.FRAUD_SUSPECTED,
    "blocked_card": FailureReason.CARD_BLOCKED,
    "cancelled_by_customer": FailureReason.CUSTOMER_CANCELLED,
    "user_cancelled": FailureReason.CUSTOMER_CANCELLED,
}


def map_failure_reason(raw: str) -> FailureReason:
    """Map an input reason string onto the known taxonomy, or UNKNOWN."""
    if raw is None:
        return FailureReason.UNKNOWN
    key = str(raw).strip().lower().replace("-", "_").replace(" ", "_")
    return _REASON_ALIASES.get(key, FailureReason.UNKNOWN)


def normalize_email(raw: Any) -> tuple[str, bool]:
    """Returns (normalized_email, is_usable)."""
    if raw is None:
        return "", False
    value = str(raw).strip().lower()
    if not value or value in {"nan", "none", "null"}:
        return "", False
    return value, bool(_EMAIL_RE.match(value))


def normalize_phone(raw: Any) -> tuple[str, bool]:
    """
    Returns (normalized_phone, is_usable). Usable numbers come back in E.164
    (+91XXXXXXXXXX) because that's what the Razorpay contact field expects.
    """
    if raw is None:
        return "", False
    raw_str = str(raw).strip()
    if not raw_str or raw_str.lower() in {"nan", "none", "null"}:
        return "", False

    digits = _PHONE_DIGITS_RE.sub("", raw_str)
    # strip country/trunk prefixes
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]

    if len(digits) == 10 and digits[0] in "6789":
        return f"+91{digits}", True
    # Keep whatever we got for the audit trail, but mark it unusable.
    return raw_str, False


def parse_amount_to_paise(raw: Any) -> Optional[int]:
    """
    Parse a rupee amount into integer paise. Returns None if unparseable or
    non-positive — we refuse to act on a case whose value we don't trust.
    """
    if raw is None:
        return None
    value = str(raw).strip().replace(",", "").replace("₹", "")
    if not value or value.lower() in {"nan", "none", "null"}:
        return None
    try:
        rupees = float(value)
    except (TypeError, ValueError):
        return None
    if rupees != rupees:  # NaN
        return None
    paise = int(round(rupees * 100))
    return paise if paise > 0 else None


def parse_timestamp(raw: Any) -> Optional[datetime]:
    """
    Parse an ISO-8601 timestamp into a tz-aware UTC datetime. Naive input is
    assumed to be UTC — the synthetic generator writes UTC, and assuming it
    keeps cooldown arithmetic from raising on mixed naive/aware comparisons.
    """
    if raw is None:
        return None
    value = str(raw).strip()
    if not value or value.lower() in {"nan", "none", "null"}:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def normalize_row(
    row_number: int,
    raw: dict[str, Any],
    seen_payment_ids: set[str],
    now: Optional[datetime] = None,
) -> tuple[Optional[FailedPayment], Optional[RejectedRecord]]:
    """
    Normalize and validate one raw row.

    Returns exactly one of (case, None) or (None, rejection).
    """
    now = now or utcnow()
    warnings: list[str] = []

    def reject(reason: RejectReason, detail: str, pid: str = "") -> tuple[None, RejectedRecord]:
        return None, RejectedRecord(
            row_number=row_number,
            payment_id=pid,
            reject_reason=reason,
            detail=detail,
            raw={k: ("" if v is None else str(v)) for k, v in raw.items()},
        )

    payment_id = str(raw.get("payment_id") or "").strip()
    if not payment_id or payment_id.lower() in {"nan", "none", "null"}:
        return reject(RejectReason.MISSING_PAYMENT_ID, "payment_id is empty — case cannot be identified")

    if payment_id in seen_payment_ids:
        return reject(
            RejectReason.DUPLICATE_PAYMENT_ID,
            f"payment_id {payment_id} already seen in this batch — refusing to risk a double contact",
            payment_id,
        )

    amount_paise = parse_amount_to_paise(raw.get("amount_inr"))
    if amount_paise is None:
        return reject(
            RejectReason.INVALID_AMOUNT,
            f"amount_inr {raw.get('amount_inr')!r} is missing, unparseable, or non-positive",
            payment_id,
        )

    created_at = parse_timestamp(raw.get("created_at"))
    if created_at is None:
        return reject(
            RejectReason.INVALID_TIMESTAMP,
            f"created_at {raw.get('created_at')!r} is missing or not ISO-8601 — cooldown cannot be computed",
            payment_id,
        )
    if created_at > now:
        warnings.append("created_at is in the future; clamped to detection time")
        created_at = now

    raw_reason = str(raw.get("failure_reason") or "").strip()
    reason = map_failure_reason(raw_reason)
    if reason is FailureReason.UNKNOWN:
        warnings.append(
            f"failure_reason {raw_reason!r} is not in the known taxonomy — routing to human review"
        )

    last_attempt_at = parse_timestamp(raw.get("last_attempt_at"))
    if last_attempt_at is not None and last_attempt_at > now:
        warnings.append("last_attempt_at is in the future; clamped to detection time")
        last_attempt_at = now
    if last_attempt_at is not None and last_attempt_at < created_at:
        warnings.append("last_attempt_at precedes created_at; treated as no prior attempt")
        last_attempt_at = None

    try:
        retry_count = int(float(str(raw.get("retry_count") or 0).strip() or 0))
    except (TypeError, ValueError):
        retry_count = 0
        warnings.append(f"retry_count {raw.get('retry_count')!r} unparseable; defaulted to 0")
    if retry_count < 0:
        warnings.append(f"retry_count {retry_count} is negative; defaulted to 0")
        retry_count = 0

    if retry_count > 0 and last_attempt_at is None:
        warnings.append("retry_count > 0 but no usable last_attempt_at; cooldown will be treated as elapsed")

    email, email_ok = normalize_email(raw.get("customer_email"))
    phone, phone_ok = normalize_phone(raw.get("customer_phone"))
    if not email_ok and not phone_ok:
        warnings.append("no usable email or phone; case is not contactable")

    currency = (str(raw.get("currency") or "INR").strip().upper()) or "INR"
    if currency != "INR":
        warnings.append(f"currency {currency!r} is not INR; amounts are reported as-is")

    try:
        case = FailedPayment(
            leak_type=LeakType.FAILED_PAYMENT,
            payment_id=payment_id,
            order_id=str(raw.get("order_id") or "").strip(),
            customer_id=str(raw.get("customer_id") or "").strip(),
            customer_name=str(raw.get("customer_name") or "").strip(),
            customer_email=email,
            customer_phone=phone,
            amount_paise=amount_paise,
            currency=currency,
            failure_reason=reason,
            raw_failure_reason=raw_reason,
            recoverability=classify_recoverability(reason),
            error_description=str(raw.get("error_description") or "").strip(),
            created_at=created_at,
            retry_count=retry_count,
            last_attempt_at=last_attempt_at,
            has_valid_email=email_ok,
            has_valid_phone=phone_ok,
            data_warnings=warnings,
        )
    except Exception as exc:  # pydantic ValidationError or anything unforeseen
        return reject(RejectReason.SCHEMA_ERROR, f"{type(exc).__name__}: {exc}", payment_id)

    seen_payment_ids.add(payment_id)
    return case, None


def read_csv_rows(path: Path) -> list[dict[str, Any]]:
    """
    Read the batch with the stdlib csv module rather than pandas: it keeps every
    field as a string so we control all coercion ourselves, and it never turns a
    blank cell into NaN behind our back.
    """
    if not path.exists():
        raise FileNotFoundError(
            f"No batch file at {path}. Run: python scripts/generate_synthetic_data.py"
        )
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def build_summary(
    cases: Iterable[FailedPayment],
    rejected: Iterable[RejectedRecord],
    max_retry_attempts: Optional[int] = None,
) -> DetectionSummary:
    cap = settings.max_retry_attempts if max_retry_attempts is None else max_retry_attempts
    summary = DetectionSummary()

    for case in cases:
        summary.total_cases += 1
        summary.total_at_risk_paise += case.amount_paise

        bucket = summary.by_reason.setdefault(
            case.failure_reason.value, ReasonBreakdown(count=0, amount_paise=0)
        )
        bucket.count += 1
        bucket.amount_paise += case.amount_paise

        if case.recoverability.value == "recoverable":
            summary.recoverable_cases += 1
            summary.recoverable_paise += case.amount_paise
        elif case.recoverability.value == "unrecoverable":
            summary.unrecoverable_cases += 1
            summary.unrecoverable_paise += case.amount_paise
        else:
            summary.unknown_cases += 1
            summary.unknown_paise += case.amount_paise

        if not case.is_contactable:
            summary.not_contactable_cases += 1
        if case.retry_count >= cap:
            summary.at_retry_cap_cases += 1
        if case.data_warnings:
            summary.cases_with_warnings += 1

    for rec in rejected:
        summary.rejected_rows += 1
        key = rec.reject_reason.value
        summary.by_reject_reason[key] = summary.by_reject_reason.get(key, 0) + 1

    return summary


def detect(
    path: Optional[Path] = None,
    rows: Optional[list[dict[str, Any]]] = None,
    now: Optional[datetime] = None,
) -> DetectionBatch:
    """
    Run the detect stage over a CSV batch (or an in-memory list of rows, which
    is what the tests use).
    """
    now = now or utcnow()
    if rows is None:
        path = path or DEFAULT_DATA_PATH
        rows = read_csv_rows(path)
        source = str(path)
    else:
        source = "in-memory"

    cases: list[FailedPayment] = []
    rejected: list[RejectedRecord] = []
    seen: set[str] = set()

    # row_number is 1-based over data rows (header excluded), so it lines up
    # with what a human sees in a spreadsheet minus the header.
    for i, raw in enumerate(rows, start=1):
        case, rejection = normalize_row(i, raw, seen, now=now)
        if case is not None:
            cases.append(case)
        if rejection is not None:
            rejected.append(rejection)

    return DetectionBatch(
        detected_at=now,
        source=source,
        rows_read=len(rows),
        cases=cases,
        rejected=rejected,
        summary=build_summary(cases, rejected),
    )
