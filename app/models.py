"""
Domain models for the Revenue Recovery agent.

Design notes that matter for the buildathon rubric:

* Money is stored canonically as **integer paise** (`amount_paise`), never as a
  float. Razorpay's API speaks paise, and integer arithmetic means the ₹ figures
  in the final metrics report are exact. `amount_inr` is a derived, display-only
  view.
* The raw input string for a failure reason is preserved (`raw_failure_reason`)
  alongside the mapped enum. An unmappable reason becomes
  `FailureReason.UNKNOWN` with `Recoverability.UNKNOWN` — the agent escalates it
  to a human rather than guessing. Guessing at an unknown cause is exactly the
  kind of unbounded behaviour the brief asks us to avoid.
* Every recovery outcome carries a `SettlementSource` so verified rupees and
  modeled rupees can never be silently blended in a metric.
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, computed_field


def utcnow() -> datetime:
    """Timezone-aware UTC now. Never use datetime.utcnow() — it returns a naive
    datetime, which blows up when compared against the tz-aware timestamps we
    parse out of the input batch."""
    return datetime.now(timezone.utc)


def paise_to_inr(paise: int) -> float:
    """Display-only conversion. Do not use the result for arithmetic."""
    return round(paise / 100, 2)


class LeakType(str, Enum):
    """
    The brief names four kinds of revenue leak. This build implements
    FAILED_PAYMENT thoroughly; the detect -> diagnose -> decide -> act -> stop
    spine is deliberately leak-type agnostic, so the others plug in by supplying
    a cause taxonomy and an action map. They are declared here to make that
    extension point explicit rather than hypothetical.
    """

    FAILED_PAYMENT = "failed_payment"
    CART_ABANDONED = "cart_abandoned"            # not implemented
    SUBSCRIPTION_FAILED = "subscription_failed"  # not implemented
    INVOICE_OVERDUE = "invoice_overdue"          # not implemented


class FailureReason(str, Enum):
    """
    Why a payment failed. These map loosely onto real Razorpay error
    codes/reasons, simplified for this build.
    """

    INSUFFICIENT_FUNDS = "insufficient_funds"
    BANK_TIMEOUT = "bank_timeout"
    GATEWAY_ERROR = "gateway_error"
    INVALID_OTP = "invalid_otp"
    CARD_EXPIRED = "card_expired"
    NETWORK_ERROR = "network_error"
    ISSUER_UNAVAILABLE = "issuer_unavailable"

    FRAUD_SUSPECTED = "fraud_suspected"
    CARD_BLOCKED = "card_blocked"
    CUSTOMER_CANCELLED = "customer_cancelled"

    UNKNOWN = "unknown"


#: Causes where a retry or a fresh payment link is legitimate.
RECOVERABLE_REASONS = frozenset({
    FailureReason.INSUFFICIENT_FUNDS,
    FailureReason.BANK_TIMEOUT,
    FailureReason.GATEWAY_ERROR,
    FailureReason.INVALID_OTP,
    FailureReason.CARD_EXPIRED,
    FailureReason.NETWORK_ERROR,
    FailureReason.ISSUER_UNAVAILABLE,
})

#: Causes the agent must NEVER retry or re-contact about.
#:   fraud_suspected    — retrying is a compliance problem, not an opportunity
#:   card_blocked       — the issuer has said no; asking again is harassment
#:   customer_cancelled — the customer expressed intent; respect it
#: This set is the hard gate. It is enforced in the policy layer, not left to
#: the LLM's discretion.
UNRECOVERABLE_REASONS = frozenset({
    FailureReason.FRAUD_SUSPECTED,
    FailureReason.CARD_BLOCKED,
    FailureReason.CUSTOMER_CANCELLED,
})


class Recoverability(str, Enum):
    RECOVERABLE = "recoverable"
    UNRECOVERABLE = "unrecoverable"
    UNKNOWN = "unknown"


def classify_recoverability(reason: "FailureReason") -> Recoverability:
    """
    Deterministic, rule-based classification. Intentionally NOT an LLM call:
    the recoverable/unrecoverable split decides whether we are allowed to touch
    a customer at all, so it must be auditable and identical on every run.
    """
    if reason in UNRECOVERABLE_REASONS:
        return Recoverability.UNRECOVERABLE
    if reason in RECOVERABLE_REASONS:
        return Recoverability.RECOVERABLE
    return Recoverability.UNKNOWN


class FailedPayment(BaseModel):
    """A single normalized, validated at-risk payment."""

    model_config = ConfigDict(extra="ignore")

    leak_type: LeakType = LeakType.FAILED_PAYMENT

    payment_id: str
    order_id: str = ""
    customer_id: str = ""
    customer_name: str = ""
    customer_email: str = ""
    customer_phone: str = ""

    amount_paise: int = Field(..., gt=0, description="Canonical money value, integer paise")
    currency: str = "INR"

    failure_reason: FailureReason
    raw_failure_reason: str = Field("", description="Original input string, preserved for audit")
    recoverability: Recoverability
    error_description: str = ""

    created_at: datetime
    retry_count: int = Field(0, ge=0)
    last_attempt_at: Optional[datetime] = None

    has_valid_email: bool = False
    has_valid_phone: bool = False

    data_warnings: list[str] = Field(
        default_factory=list,
        description="Non-fatal normalization findings, carried into the audit trail",
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def amount_inr(self) -> float:
        """Display-only."""
        return paise_to_inr(self.amount_paise)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_contactable(self) -> bool:
        """
        Whether we have any channel to reach this customer on. A case with no
        usable email or phone cannot be sent a payment link, no matter how
        recoverable its cause is — it has to go to a human.
        """
        return self.has_valid_email or self.has_valid_phone


class RejectReason(str, Enum):
    """
    Why a raw input row could not become a case at all. These are hard data
    faults — the agent cannot reason about a record it cannot identify or
    whose amount it cannot trust.
    """

    MISSING_PAYMENT_ID = "missing_payment_id"
    DUPLICATE_PAYMENT_ID = "duplicate_payment_id"
    INVALID_AMOUNT = "invalid_amount"
    INVALID_TIMESTAMP = "invalid_timestamp"
    SCHEMA_ERROR = "schema_error"


class RejectedRecord(BaseModel):
    """A row that failed ingestion. Never silently dropped — always reported."""

    row_number: int
    payment_id: str = ""
    reject_reason: RejectReason
    detail: str
    raw: dict = Field(default_factory=dict)


class ReasonBreakdown(BaseModel):
    count: int
    amount_paise: int

    @computed_field  # type: ignore[prop-decorator]
    @property
    def amount_inr(self) -> float:
        return paise_to_inr(self.amount_paise)


class DetectionSummary(BaseModel):
    """Aggregates over one detection run."""

    total_cases: int = 0
    total_at_risk_paise: int = 0

    recoverable_cases: int = 0
    recoverable_paise: int = 0
    unrecoverable_cases: int = 0
    unrecoverable_paise: int = 0
    unknown_cases: int = 0
    unknown_paise: int = 0

    rejected_rows: int = 0

    not_contactable_cases: int = 0
    at_retry_cap_cases: int = 0
    cases_with_warnings: int = 0

    by_reason: dict[str, ReasonBreakdown] = Field(default_factory=dict)
    by_reject_reason: dict[str, int] = Field(default_factory=dict)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_at_risk_inr(self) -> float:
        return paise_to_inr(self.total_at_risk_paise)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def recoverable_inr(self) -> float:
        return paise_to_inr(self.recoverable_paise)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def unrecoverable_inr(self) -> float:
        """₹ the agent will deliberately NOT chase. Reported as a headline
        number, not hidden — restraint is a result."""
        return paise_to_inr(self.unrecoverable_paise)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def unknown_inr(self) -> float:
        return paise_to_inr(self.unknown_paise)


class DetectionBatch(BaseModel):
    """Output of the detect stage: what's at risk, what couldn't be read."""

    detected_at: datetime = Field(default_factory=utcnow)
    source: str
    rows_read: int = 0
    cases: list[FailedPayment] = Field(default_factory=list)
    rejected: list[RejectedRecord] = Field(default_factory=list)
    summary: DetectionSummary = Field(default_factory=DetectionSummary)


class ActionType(str, Enum):
    """
    The complete, closed set of things the agent may do. Adding a capability
    means adding a member here AND a corresponding bounded function in
    services/razorpay_client.py — there is no general-purpose escape hatch.
    """

    RETRY_SAME_METHOD = "retry_same_method"
    SEND_ALT_PAYMENT_LINK = "send_alt_payment_link"
    ESCALATE_TO_HUMAN = "escalate_to_human"
    STOP_NO_ACTION = "stop_no_action"


class SettlementSource(str, Enum):
    """
    Provenance of a recovered rupee. Verified and modeled amounts are reported
    in separate columns and are never summed into a single figure.
    """

    VERIFIED_API = "verified_api"  # Razorpay test-mode API confirmed the link was paid
    MODELED = "modeled"            # resolved by the seeded outcome model
    NONE = "none"                  # nothing recovered


class AgentDecision(BaseModel):
    """One decision about one case. Extended in Day 3 (diagnose) / Day 4 (act)."""

    payment_id: str
    detected_reason: FailureReason
    recoverability: Recoverability
    action: ActionType
    reasoning: str = Field(..., description="Why this action was chosen")
    action_result: Optional[str] = None
    recovered_paise: int = 0
    settlement_source: SettlementSource = SettlementSource.NONE
    decided_at: datetime = Field(default_factory=utcnow)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def recovered_inr(self) -> float:
        return paise_to_inr(self.recovered_paise)
