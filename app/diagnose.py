"""
Diagnose stage: work out *why* a case failed and describe it in language a human
reviewer can audit.

The single invariant this module exists to enforce:

    **The LLM can never widen the agent's permissions. It can only narrow them.**

Concretely:

* A case the deterministic gate already marked ``UNRECOVERABLE`` is never sent to
  the model at all (``DiagnosisSource.GATE``). We don't pay a model to opine on a
  case we are forbidden to touch, and we don't give it the chance to argue.
* A ``RECOVERABLE`` case is sent. If the model reads it as fraud / a blocked card
  / a customer who cancelled, the case is **downgraded** to unrecoverable and
  stops. That direction is always honoured, because it is the safe one.
* An ``UNKNOWN`` cause is sent, because resolving an unmapped reason string is
  something a language model is genuinely good at. But its answer is only
  accepted when it lands on an unrecoverable cause. If it claims an unknown
  string is really something recoverable, the case **stays UNKNOWN and goes to a
  human** — the model's reading is attached as context for that human, not acted
  on. Letting a model unlock a payment attempt by relabelling an unrecognised
  error is exactly the unbounded behaviour the brief asks us to avoid.

Every refused suggestion is recorded in ``Diagnosis.boundary_violations`` and
counted in the metrics report, so "the LLM tried to overstep N times" is a
measured number rather than a reassurance.

Cost note: identical ``(reason, error_description)`` pairs repeat heavily across
a real batch, so diagnoses are cached on that signature within a run. An 88-row
batch costs roughly a dozen calls, not eighty-eight.
"""

from __future__ import annotations

import json
import re
from typing import Optional

from app.config import settings
from app.models import (
    CUSTOMER_ACTION_REASONS,
    TRANSIENT_REASONS,
    UNRECOVERABLE_REASONS,
    ActionType,
    Diagnosis,
    DiagnosisSource,
    FailedPayment,
    FailureReason,
    Recoverability,
    classify_recoverability,
    utcnow,
)
from app.services import llm_client

_ALLOWED_CAUSES = [r.value for r in FailureReason]
_ALLOWED_ACTIONS = [a.value for a in ActionType]

SYSTEM_PROMPT = """You are the diagnosis step of an automated payment-recovery \
agent for an Indian merchant. You classify why a payment failed. You do not \
authorise anything: a separate deterministic policy layer decides what action is \
taken, and it will overrule you.

Rules you must follow:
1. Choose `cause` from this exact list, nothing else: {causes}
2. If the evidence does not clearly support one of those causes, use "unknown". \
Guessing is worse than admitting you don't know.
3. If the evidence suggests fraud, a card blocked by the issuer, or a customer \
who deliberately cancelled, say so plainly. Those cases will be dropped from the \
recovery workflow entirely, which is the correct outcome.
4. `recommended_action` must come from this list: {actions}
5. Reply with a single JSON object and no other text. No markdown fences, no \
commentary.

Schema:
{{"cause": str, "likely_transient": bool, "confidence": float 0-1,
  "root_cause": str (<=200 chars, plain English, no customer PII),
  "recommended_action": str}}""".format(
    causes=", ".join(_ALLOWED_CAUSES), actions=", ".join(_ALLOWED_ACTIONS)
)


#: Rule-based narration used when the LLM is unavailable, and as the baseline
#: description for gated cases. Keyed by cause so the audit trail always carries
#: a sentence explaining the case, LLM or no LLM.
_RULE_NARRATION: dict[FailureReason, str] = {
    FailureReason.INSUFFICIENT_FUNDS: (
        "Account lacked the balance to cover the charge. The instrument is fine, "
        "so the customer needs a link they can pay once funded."
    ),
    FailureReason.BANK_TIMEOUT: (
        "The bank did not answer inside the timeout window. Nothing is wrong with "
        "the card; a later re-presentment of the same method is likely to clear."
    ),
    FailureReason.GATEWAY_ERROR: (
        "The gateway returned a temporary processing error. Transient and on the "
        "processor's side, so re-presenting the same method is reasonable."
    ),
    FailureReason.NETWORK_ERROR: (
        "The authorisation was interrupted in transit. No decline was issued by "
        "the issuer, so the same method can be tried again."
    ),
    FailureReason.ISSUER_UNAVAILABLE: (
        "The issuing bank's systems were unavailable. An infrastructure outage, "
        "not a decline — the same method should be retried once it recovers."
    ),
    FailureReason.INVALID_OTP: (
        "Authorisation failed because the OTP entered was wrong. The customer has "
        "to complete authentication again, which needs a fresh checkout."
    ),
    FailureReason.CARD_EXPIRED: (
        "The card's expiry date has passed. Re-charging it would fail identically, "
        "so the customer must supply a different instrument."
    ),
    FailureReason.FRAUD_SUSPECTED: (
        "The risk engine blocked this payment as suspected fraud. Retrying a "
        "risk-blocked payment is a compliance problem, not a revenue opportunity."
    ),
    FailureReason.CARD_BLOCKED: (
        "The issuing bank has blocked the card. The issuer has already said no; "
        "asking the customer again is harassment, not recovery."
    ),
    FailureReason.CUSTOMER_CANCELLED: (
        "The customer abandoned the payment deliberately. They expressed intent, "
        "and that intent is respected."
    ),
    FailureReason.UNKNOWN: (
        "The failure reason is not in the known taxonomy, so no cause can be "
        "asserted. A human decides this one; the agent does not guess."
    ),
}


def signature(case: FailedPayment) -> str:
    """
    Cache key for a diagnosis. Two cases with the same mapped cause, the same raw
    reason string and the same error text will get the same reading, so there is
    no point paying for the call twice.

    Deliberately excludes amount, customer and retry count: those feed the
    *decide* stage, which is deterministic, not the *diagnose* stage.
    """
    return "|".join([
        case.failure_reason.value,
        case.raw_failure_reason.strip().lower(),
        case.error_description.strip().lower(),
    ])


def build_user_prompt(case: FailedPayment) -> str:
    """
    Assemble the evidence for one case.

    No name, no email, no phone: the model does not need identity to classify a
    failure cause, so it does not get it. Sending customer PII to a third party
    when the task doesn't require it is a habit worth not forming.
    """
    hours_old = round((utcnow() - case.created_at).total_seconds() / 3600, 1)
    lines = [
        "Classify this failed payment.",
        "",
        f"gateway_failure_reason: {case.raw_failure_reason or '(blank)'}",
        f"gateway_error_description: {case.error_description or '(blank)'}",
        f"amount_inr: {case.amount_inr}",
        f"prior_attempts: {case.retry_count}",
        f"hours_since_failure: {hours_old}",
        f"reachable_by_email: {case.has_valid_email}",
        f"reachable_by_sms: {case.has_valid_phone}",
    ]
    if case.data_warnings:
        lines.append(f"data_quality_warnings: {'; '.join(case.data_warnings)}")
    return "\n".join(lines)


def _extract_json(text: str) -> Optional[dict]:
    """
    Pull a JSON object out of a model reply. Tolerates markdown fences and stray
    prose around the object, because "reply with only JSON" is an instruction,
    not a guarantee.
    """
    if not text:
        return None
    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        parsed = json.loads(cleaned)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def _coerce_cause(raw: object) -> Optional[FailureReason]:
    """Accept a cause only if it is verbatim inside the taxonomy."""
    if not isinstance(raw, str):
        return None
    try:
        return FailureReason(raw.strip().lower())
    except ValueError:
        return None


def _coerce_action(raw: object) -> Optional[ActionType]:
    if not isinstance(raw, str):
        return None
    try:
        return ActionType(raw.strip().lower())
    except ValueError:
        return None


def _coerce_confidence(raw: object) -> float:
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    return min(1.0, max(0.0, value))


def rule_diagnosis(case: FailedPayment, source: DiagnosisSource, note: str = "") -> Diagnosis:
    """
    Deterministic diagnosis straight from the taxonomy. Used for gated cases and
    whenever the LLM path is unavailable. Confidence is 1.0 and that is not
    bravado: the mapping is a lookup table, so it is exactly as certain as the
    input reason string.
    """
    root_cause = _RULE_NARRATION.get(case.failure_reason, "")
    if note:
        root_cause = f"{root_cause} [{note}]".strip()
    return Diagnosis(
        payment_id=case.payment_id,
        reason=case.failure_reason,
        recoverability=case.recoverability,
        root_cause=root_cause,
        likely_transient=case.failure_reason in TRANSIENT_REASONS,
        confidence=1.0 if case.failure_reason is not FailureReason.UNKNOWN else 0.0,
        suggested_action=None,
        source=source,
    )


def _apply_llm_reading(case: FailedPayment, parsed: dict, raw_text: str) -> Diagnosis:
    """
    Turn a parsed model reply into a Diagnosis, enforcing the one-way permission
    rule. This function is where the LLM's authority is bounded; everything
    before it is plumbing.
    """
    violations: list[str] = []

    llm_cause = _coerce_cause(parsed.get("cause"))
    if llm_cause is None:
        violations.append(
            f"cause {parsed.get('cause')!r} is outside the taxonomy; deterministic cause kept"
        )

    final_reason = case.failure_reason
    final_recoverability = case.recoverability

    if llm_cause is not None and llm_cause is not case.failure_reason:
        if llm_cause in UNRECOVERABLE_REASONS:
            # Narrowing: the model sees a reason to stop that the reason string
            # did not carry. Always honoured — this direction only ever removes
            # permission.
            final_reason = llm_cause
            final_recoverability = Recoverability.UNRECOVERABLE
            violations.append(
                f"cause downgraded {case.failure_reason.value} -> {llm_cause.value} "
                "(model identified an unrecoverable cause; honoured, stops the case)"
            )
        elif case.recoverability is Recoverability.UNKNOWN:
            # Widening: the model wants to relabel an unrecognised error as
            # something actionable. Refused — the case stays UNKNOWN and a human
            # decides. The reading is kept as context for that human.
            violations.append(
                f"refused to relabel an unknown cause as {llm_cause.value}: an LLM may not "
                "unlock a payment attempt for a reason string the taxonomy does not recognise"
            )
        else:
            # Both sides recoverable but disagreeing. The deterministic mapping of
            # the gateway's own reason string is the better evidence.
            violations.append(
                f"cause disagreement {case.failure_reason.value} vs {llm_cause.value}; "
                "gateway reason string kept"
            )

    suggested = _coerce_action(parsed.get("recommended_action"))
    if parsed.get("recommended_action") is not None and suggested is None:
        violations.append(
            f"recommended_action {parsed.get('recommended_action')!r} is not a permitted action"
        )
    if suggested is not None and final_recoverability is not Recoverability.RECOVERABLE:
        violations.append(
            f"suggested {suggested.value} on a {final_recoverability.value} case; discarded"
        )
        suggested = None

    root_cause = str(parsed.get("root_cause") or "").strip()[:400]
    if not root_cause:
        root_cause = _RULE_NARRATION.get(final_reason, "")

    likely_transient = bool(parsed.get("likely_transient", final_reason in TRANSIENT_REASONS))
    if final_recoverability is Recoverability.UNRECOVERABLE:
        # A gated cause is never "transient"; the block is the point.
        likely_transient = False

    return Diagnosis(
        payment_id=case.payment_id,
        reason=final_reason,
        recoverability=final_recoverability,
        root_cause=root_cause,
        likely_transient=likely_transient,
        confidence=_coerce_confidence(parsed.get("confidence")),
        suggested_action=suggested,
        source=DiagnosisSource.LLM,
        model=settings.fireworks_model,
        llm_raw=raw_text[:2000],
        boundary_violations=violations,
    )


class DiagnoseSession:
    """
    Run-scoped state for the diagnose stage: a signature cache and a circuit
    breaker.

    The breaker earns its place. A bad API key returns 401 on every call, and
    without it an 88-row batch becomes 88 slow round-trips that all fail the same
    way. After `failure_budget` consecutive failures the stage stops calling out
    and finishes the batch on the rule path, recording why. Degrading fast and
    saying so beats degrading slowly and silently.
    """

    def __init__(self, use_llm: bool = True, failure_budget: int = 2) -> None:
        self.cache: dict[str, Diagnosis] = {}
        self.use_llm = use_llm
        self.failure_budget = failure_budget
        self.failures = 0
        self.tripped_reason = ""
        self.calls_made = 0
        self.cache_hits = 0

    @property
    def llm_enabled(self) -> bool:
        return self.use_llm and not self.tripped_reason

    def record_failure(self, error: str) -> None:
        self.failures += 1
        if self.failures >= self.failure_budget:
            self.tripped_reason = (
                f"LLM disabled after {self.failures} consecutive failures; last error: {error}"
            )

    def record_success(self) -> None:
        self.failures = 0
        self.calls_made += 1


def diagnose(
    case: FailedPayment,
    use_llm: bool = True,
    session: Optional[DiagnoseSession] = None,
) -> Diagnosis:
    """
    Diagnose one case.

    `session` is owned by the caller — the orchestrator creates one per run so the
    cache and circuit breaker are shared across the batch. Passing None gives a
    single-shot diagnosis with neither, which is what the unit tests want.
    """
    # 1. Hard gate first. An unrecoverable case never reaches the model.
    if case.recoverability is Recoverability.UNRECOVERABLE:
        return rule_diagnosis(case, DiagnosisSource.GATE)

    if not use_llm:
        return rule_diagnosis(case, DiagnosisSource.RULE_FALLBACK, "LLM disabled for this run")

    if session is not None and not session.llm_enabled:
        return rule_diagnosis(case, DiagnosisSource.RULE_FALLBACK, session.tripped_reason)

    if not llm_client.is_available():
        return rule_diagnosis(case, DiagnosisSource.RULE_FALLBACK, llm_client.unavailable_reason())

    key = signature(case)
    if session is not None and key in session.cache:
        session.cache_hits += 1
        return session.cache[key].model_copy(
            update={
                "payment_id": case.payment_id,
                "source": DiagnosisSource.LLM_CACHE,
                "diagnosed_at": utcnow(),
            }
        )

    text, error = llm_client.complete(SYSTEM_PROMPT, build_user_prompt(case))
    if error:
        if session is not None:
            session.record_failure(error)
        return rule_diagnosis(case, DiagnosisSource.RULE_FALLBACK, f"LLM call failed: {error}")

    parsed = _extract_json(text)
    if parsed is None:
        if session is not None:
            session.record_failure("unparseable JSON reply")
        diagnosis = rule_diagnosis(
            case, DiagnosisSource.RULE_FALLBACK, "LLM reply was not parseable JSON"
        )
        return diagnosis.model_copy(update={"llm_raw": text[:2000]})

    if session is not None:
        session.record_success()
    diagnosis = _apply_llm_reading(case, parsed, text)
    if session is not None:
        session.cache[key] = diagnosis
    return diagnosis


def diagnose_all(
    cases: list[FailedPayment],
    use_llm: bool = True,
    session: Optional[DiagnoseSession] = None,
) -> list[Diagnosis]:
    """Diagnose a batch, sharing one cache and one circuit breaker across it."""
    session = session or DiagnoseSession(use_llm=use_llm)
    return [diagnose(case, use_llm=use_llm, session=session) for case in cases]


__all__ = [
    "CUSTOMER_ACTION_REASONS",
    "SYSTEM_PROMPT",
    "TRANSIENT_REASONS",
    "DiagnoseSession",
    "build_user_prompt",
    "diagnose",
    "diagnose_all",
    "rule_diagnosis",
    "signature",
]
