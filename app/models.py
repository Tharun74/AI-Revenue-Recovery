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

#: Recoverable causes that are somebody else's infrastructure having a bad
#: moment. Nothing about the customer or their card is wrong, so re-presenting
#: the same instrument is a fair thing to do.
TRANSIENT_REASONS = frozenset({
    FailureReason.BANK_TIMEOUT,
    FailureReason.GATEWAY_ERROR,
    FailureReason.NETWORK_ERROR,
    FailureReason.ISSUER_UNAVAILABLE,
})

#: Recoverable causes where the customer has to *do* something — fund the
#: account, use a different card, complete authorisation again. Re-charging the
#: same instrument would fail identically, so these get a payment link instead.
CUSTOMER_ACTION_REASONS = frozenset({
    FailureReason.INSUFFICIENT_FUNDS,
    FailureReason.CARD_EXPIRED,
    FailureReason.INVALID_OTP,
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


# --------------------------------------------------------------------------
# Diagnose stage
# --------------------------------------------------------------------------

class DiagnosisSource(str, Enum):
    """
    Where a diagnosis came from. Recorded on every case so a reviewer can see
    exactly how much of the run the LLM actually influenced.

    GATE means the deterministic gate resolved the case and **no LLM call was
    made at all** — we do not pay a model to opine on a case we are forbidden to
    touch, and we do not give it the chance to argue.
    """

    GATE = "gate"
    LLM = "llm"
    LLM_CACHE = "llm_cache"        # identical (reason, error text) signature already diagnosed
    RULE_FALLBACK = "rule_fallback"  # no key, call failed, or reply was unusable


class Diagnosis(BaseModel):
    """
    Root-cause reading of one case.

    `suggested_action` is **advisory**. The decide stage may only honour it when
    it already agrees with the deterministic action map; any other suggestion is
    discarded and recorded in `boundary_violations`. The LLM narrates, it does
    not authorise.
    """

    payment_id: str
    reason: FailureReason = Field(..., description="Final cause, always inside the taxonomy")
    recoverability: Recoverability
    root_cause: str = Field("", description="Human-readable explanation for the audit trail")
    likely_transient: bool = False
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    suggested_action: Optional[ActionType] = Field(
        None, description="Advisory only — never executed without the policy map agreeing"
    )
    source: DiagnosisSource
    model: str = ""
    llm_raw: str = Field("", description="Verbatim model reply, preserved for audit")
    boundary_violations: list[str] = Field(
        default_factory=list,
        description="Things the model proposed that the deterministic layer refused",
    )
    diagnosed_at: datetime = Field(default_factory=utcnow)


# --------------------------------------------------------------------------
# Decide stage
# --------------------------------------------------------------------------

class PolicyRule(str, Enum):
    """
    The deterministic rule that produced a decision. Rules are evaluated in a
    fixed, documented order and the first match wins, so every decision can be
    traced to exactly one rule — see app/decide.py.
    """

    #: Hard gate. fraud_suspected / card_blocked / customer_cancelled.
    GATE_UNRECOVERABLE_CAUSE = "gate_unrecoverable_cause"
    #: Cause outside the known taxonomy — a human decides, the agent does not guess.
    ESCALATE_UNKNOWN_CAUSE = "escalate_unknown_cause"
    #: No usable email or phone, so no action could reach the customer anyway.
    ESCALATE_NOT_CONTACTABLE = "escalate_not_contactable"
    #: Already touched max_retry_attempts times. Stop contacting, hand over.
    ESCALATE_RETRY_CAP_REACHED = "escalate_retry_cap_reached"
    #: Inside the cooldown window since the last attempt. Not now.
    STOP_COOLDOWN_NOT_ELAPSED = "stop_cooldown_not_elapsed"
    #: Transient infrastructure fault — re-presenting the same instrument is fair.
    RETRY_TRANSIENT_FAULT = "retry_transient_fault"
    #: The customer has to do something (fund the account, use another card,
    #: redo authorisation), so send them a link rather than re-charging blindly.
    LINK_CUSTOMER_ACTION_REQUIRED = "link_customer_action_required"
    #: A recoverable cause with no entry in the action map. Fail closed.
    ESCALATE_NO_ACTION_MAPPED = "escalate_no_action_mapped"


class Decision(BaseModel):
    """One decision about one case, with the rule that produced it."""

    payment_id: str
    amount_paise: int
    reason: FailureReason
    recoverability: Recoverability
    action: ActionType
    policy_rule: PolicyRule
    reasoning: str = Field(..., description="Why this action was chosen, in plain English")
    contact_channel: str = Field("none", description="email | sms | email+sms | none")
    llm_suggestion_honoured: Optional[bool] = Field(
        None, description="None when the LLM was never consulted for this case"
    )
    decided_at: datetime = Field(default_factory=utcnow)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def amount_inr(self) -> float:
        return paise_to_inr(self.amount_paise)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def contacts_customer(self) -> bool:
        """
        Whether executing this decision reaches out to the customer. Escalating
        to a human and stopping do not — which is why they are always safe.
        """
        return self.action in {ActionType.RETRY_SAME_METHOD, ActionType.SEND_ALT_PAYMENT_LINK}


# --------------------------------------------------------------------------
# Act stage
# --------------------------------------------------------------------------

class ActionStatus(str, Enum):
    EXECUTED = "executed"    # a real Razorpay test-mode call succeeded
    SIMULATED = "simulated"  # dry run — nothing left the process
    NO_ACTION = "no_action"  # STOP_NO_ACTION: deliberately did nothing
    ESCALATED = "escalated"  # handed to a human; no customer contact made
    FAILED = "failed"        # a real call was attempted and errored


class ActionOutcome(BaseModel):
    """
    What actually happened when a decision was executed, and what — if anything —
    was recovered.

    `settlement_source` is mandatory and never defaults to something flattering:
    only a Razorpay API confirmation yields VERIFIED_API.
    """

    payment_id: str
    action: ActionType
    status: ActionStatus
    provider_object: str = Field("", description="payment_link | order | '' ")
    provider_ref: str = Field("", description="Razorpay id, e.g. plink_… / order_…")
    provider_short_url: str = Field("", description="Payable link, when one was created")
    detail: str = ""
    error: str = ""
    recovered_paise: int = 0
    settlement_source: SettlementSource = SettlementSource.NONE
    settlement_detail: str = ""
    executed_at: datetime = Field(default_factory=utcnow)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def recovered_inr(self) -> float:
        return paise_to_inr(self.recovered_paise)


class CaseRecord(BaseModel):
    """Everything that happened to one case in one run, end to end."""

    case: FailedPayment
    diagnosis: Diagnosis
    decision: Decision
    outcome: ActionOutcome


# --------------------------------------------------------------------------
# Audit trail
# --------------------------------------------------------------------------

class AuditStage(str, Enum):
    RUN_STARTED = "run_started"
    ROW_REJECTED = "row_rejected"
    CASE_DETECTED = "case_detected"
    DIAGNOSED = "diagnosed"
    DECIDED = "decided"
    ACTED = "acted"
    SETTLED = "settled"
    RUN_COMPLETED = "run_completed"


class AuditEvent(BaseModel):
    """
    One append-only audit entry.

    Entries are hash-chained: `entry_hash` covers the event *and* `prev_hash`,
    so silently editing or deleting history breaks verification downstream. See
    app/audit.py.
    """

    seq: int
    run_id: str
    at: datetime
    stage: AuditStage
    payment_id: str = ""
    summary: str = ""
    payload: dict = Field(default_factory=dict)
    prev_hash: str = ""
    entry_hash: str = ""


class AuditVerification(BaseModel):
    """
    Result of re-walking the hash chain.

    `ok=False` does not mean the agent misbehaved — it means the record of what
    the agent did can no longer be trusted, which for an audit trail is the same
    severity of problem.
    """

    path: str
    events: int = 0
    ok: bool = True
    broken_at_seq: Optional[int] = None
    detail: str = ""


# --------------------------------------------------------------------------
# Metrics report
# --------------------------------------------------------------------------

class MoneyLine(BaseModel):
    """A (cases, ₹) pair. Used for every line of the report so nothing is a
    bare number without a case count behind it."""

    cases: int = 0
    amount_paise: int = 0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def amount_inr(self) -> float:
        return paise_to_inr(self.amount_paise)

    def add(self, amount_paise: int) -> None:
        self.cases += 1
        self.amount_paise += amount_paise


class ExceptionItem(BaseModel):
    """An unresolved problem, itemised rather than aggregated away."""

    kind: str = Field(..., description="rejected_row | action_failed")
    reference: str = Field(..., description="payment_id, or row N for an unreadable row")
    detail: str
    amount_paise: int = 0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def amount_inr(self) -> float:
        return paise_to_inr(self.amount_paise)


class MetricsReport(BaseModel):
    """
    The batch report.

    The one rule this model enforces structurally: **there is no field that sums
    verified and modeled rupees.** `recovered_verified` and `recovered_modeled`
    are separate lines with separate rates, because a single blended
    "₹ recovered" would be the easiest number in this project to disbelieve.
    """

    run_id: str
    generated_at: datetime = Field(default_factory=utcnow)
    source: str = ""
    dry_run: bool = True
    llm_used: bool = False
    rows_read: int = 0
    #: True when only the first N cases were processed. Recorded because the
    #: "every input row is accounted for" invariant legitimately cannot hold on a
    #: truncated run, and a false alarm in the invariant panel is worse than no
    #: panel at all.
    partial_run: bool = False

    at_risk: MoneyLine = Field(default_factory=MoneyLine)
    recoverable: MoneyLine = Field(default_factory=MoneyLine)
    unrecoverable: MoneyLine = Field(default_factory=MoneyLine)
    unknown_cause: MoneyLine = Field(default_factory=MoneyLine)

    #: Cases the agent actually reached out on this run.
    attempted: MoneyLine = Field(default_factory=MoneyLine)
    #: Razorpay confirmed the link was paid. Never mixed with the line below.
    recovered_verified: MoneyLine = Field(default_factory=MoneyLine)
    #: Resolved by the seeded outcome model. Never mixed with the line above.
    recovered_modeled: MoneyLine = Field(default_factory=MoneyLine)
    #: Restraint, as a headline figure.
    deliberately_not_chased: MoneyLine = Field(default_factory=MoneyLine)
    #: Recoverable money the agent held back this run (cooldown, retry cap).
    withheld_this_run: MoneyLine = Field(default_factory=MoneyLine)

    gated_cases: int = 0
    correctly_stopped_cases: int = 0

    escalations: MoneyLine = Field(default_factory=MoneyLine)
    escalations_by_rule: dict[str, int] = Field(default_factory=dict)
    by_action: dict[str, int] = Field(default_factory=dict)
    by_policy_rule: dict[str, int] = Field(default_factory=dict)
    by_diagnosis_source: dict[str, int] = Field(default_factory=dict)

    customer_contacts_made: int = 0
    boundary_violations_refused: int = 0

    unresolved_exceptions: list[ExceptionItem] = Field(default_factory=list)
    reconciliation: dict[str, bool] = Field(default_factory=dict)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def stop_compliance_pct(self) -> float:
        """
        Share of gated causes that exited with STOP_NO_ACTION. Anything below
        100.0 is a failed run, not a low score.
        """
        if self.gated_cases == 0:
            return 100.0
        return round(100 * self.correctly_stopped_cases / self.gated_cases, 2)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def recovery_rate_verified_pct(self) -> float:
        """Verified ₹ as a share of ₹ actually attempted. Denominator is the
        money we reached out on — not the whole batch, which would flatter it."""
        if self.attempted.amount_paise == 0:
            return 0.0
        return round(100 * self.recovered_verified.amount_paise / self.attempted.amount_paise, 2)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def recovery_rate_modeled_pct(self) -> float:
        if self.attempted.amount_paise == 0:
            return 0.0
        return round(100 * self.recovered_modeled.amount_paise / self.attempted.amount_paise, 2)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def unresolved_exception_count(self) -> int:
        return len(self.unresolved_exceptions)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def all_invariants_hold(self) -> bool:
        return all(self.reconciliation.values()) if self.reconciliation else False


class AgentRun(BaseModel):
    """Output of one full detect → diagnose → decide → act → stop pass."""

    run_id: str
    started_at: datetime
    finished_at: datetime
    dry_run: bool
    source: str
    detection: DetectionSummary
    records: list[CaseRecord] = Field(default_factory=list)
    metrics: MetricsReport
