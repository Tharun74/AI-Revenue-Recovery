"""
Decide stage: turn a diagnosis into exactly one bounded action, and name the rule
that did it.

This module is deliberately dull. There is no model call here, no randomness and
no branching on anything but the case's own fields. The decide stage is where
"the agent will not harass anyone" stops being a claim and becomes control flow,
so it has to be readable by someone who does not trust us.

Two structural guarantees:

**1. Exactly one rule fires, and it is recorded.** Rules are evaluated in the
fixed order below and the first match wins. Every `Decision` carries the
`PolicyRule` that produced it, so no decision in the audit trail is unexplained.

    1. GATE_UNRECOVERABLE_CAUSE     fraud / blocked card / customer cancelled  -> STOP
    2. ESCALATE_UNKNOWN_CAUSE       cause outside the taxonomy                 -> HUMAN
    3. ESCALATE_NOT_CONTACTABLE     no usable email or phone                   -> HUMAN
    4. ESCALATE_RETRY_CAP_REACHED   already touched max_retry_attempts times   -> HUMAN
    5. STOP_COOLDOWN_NOT_ELAPSED    inside the cooldown window                 -> STOP
    6. RETRY_TRANSIENT_FAULT        someone else's outage                      -> RETRY
    7. LINK_CUSTOMER_ACTION_REQUIRED the customer must do something            -> LINK
    8. ESCALATE_NO_ACTION_MAPPED    recoverable but unmapped; fail closed      -> HUMAN

The two hard stops are checked before anything else, and the two "actually
contact someone" rules are checked last. That ordering is the safety property: a
case cannot reach a contact rule without having passed every gate.

**2. The LLM cannot widen permissions here either.** `effective_recoverability`
takes the *stricter* of the detect stage's classification and the diagnosis's, so
even a diagnose-stage bug cannot promote an unrecoverable case into an actionable
one. app/diagnose.py enforces this too; enforcing it twice is cheap, and the
place where money moves is the wrong place to rely on a caller behaving.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from app.config import settings
from app.models import (
    CUSTOMER_ACTION_REASONS,
    RECOVERABLE_REASONS,
    TRANSIENT_REASONS,
    ActionType,
    Decision,
    Diagnosis,
    DiagnosisSource,
    FailedPayment,
    PolicyRule,
    Recoverability,
    utcnow,
)

#: cause -> (action, rule). Built from the taxonomy sets in models.py rather than
#: hand-listed, so a new recoverable cause cannot quietly arrive without an
#: action. `unmapped_recoverable_reasons()` is asserted empty by the test suite.
ACTION_MAP: dict = {
    **{r: (ActionType.RETRY_SAME_METHOD, PolicyRule.RETRY_TRANSIENT_FAULT)
       for r in TRANSIENT_REASONS},
    **{r: (ActionType.SEND_ALT_PAYMENT_LINK, PolicyRule.LINK_CUSTOMER_ACTION_REQUIRED)
       for r in CUSTOMER_ACTION_REASONS},
}

#: Strictness ordering. Lower index = fewer permissions. Used to take the
#: stricter of two readings, never the more permissive one.
_STRICTNESS = [
    Recoverability.UNRECOVERABLE,
    Recoverability.UNKNOWN,
    Recoverability.RECOVERABLE,
]


def unmapped_recoverable_reasons() -> set:
    """Recoverable causes with no action mapped. Must be empty."""
    return set(RECOVERABLE_REASONS) - set(ACTION_MAP)


def effective_recoverability(case: FailedPayment, diagnosis: Diagnosis) -> Recoverability:
    """
    The stricter of the two readings. A one-way ratchet: diagnosis may remove
    permission, never grant it.
    """
    return min(
        (case.recoverability, diagnosis.recoverability),
        key=lambda r: _STRICTNESS.index(r),
    )


def contact_channel(case: FailedPayment) -> str:
    if case.has_valid_email and case.has_valid_phone:
        return "email+sms"
    if case.has_valid_email:
        return "email"
    if case.has_valid_phone:
        return "sms"
    return "none"


def cooldown_remaining(
    case: FailedPayment, now: Optional[datetime] = None, cooldown_hours: Optional[int] = None
) -> Optional[timedelta]:
    """
    How long until this case may be touched again, or None if it is free to touch.

    A case with prior attempts but no usable `last_attempt_at` is treated as
    free. The detect stage already flags that data gap as a warning; blocking on
    a missing timestamp would strand recoverable money on a clerical error.
    """
    now = now or utcnow()
    hours = settings.retry_cooldown_hours if cooldown_hours is None else cooldown_hours
    if case.last_attempt_at is None:
        return None
    elapsed = now - case.last_attempt_at
    window = timedelta(hours=hours)
    return window - elapsed if elapsed < window else None


def _fmt_hours(delta: timedelta) -> str:
    return f"{delta.total_seconds() / 3600:.1f}h"


def decide(
    case: FailedPayment,
    diagnosis: Diagnosis,
    now: Optional[datetime] = None,
    max_retry_attempts: Optional[int] = None,
    cooldown_hours: Optional[int] = None,
) -> Decision:
    """Apply the policy rules in order and return the first match."""
    now = now or utcnow()
    cap = settings.max_retry_attempts if max_retry_attempts is None else max_retry_attempts
    recoverability = effective_recoverability(case, diagnosis)

    # The LLM is only "consulted" in a meaningful sense when it actually ran.
    consulted = diagnosis.source in {DiagnosisSource.LLM, DiagnosisSource.LLM_CACHE}

    def build(action: ActionType, rule: PolicyRule, reasoning: str) -> Decision:
        honoured: Optional[bool] = None
        if consulted and diagnosis.suggested_action is not None:
            honoured = diagnosis.suggested_action is action
        return Decision(
            payment_id=case.payment_id,
            amount_paise=case.amount_paise,
            reason=diagnosis.reason,
            recoverability=recoverability,
            action=action,
            policy_rule=rule,
            reasoning=reasoning,
            contact_channel=contact_channel(case),
            llm_suggestion_honoured=honoured,
        )

    # 1. The hard gate. Nothing downstream of here can undo it.
    if recoverability is Recoverability.UNRECOVERABLE:
        return build(
            ActionType.STOP_NO_ACTION,
            PolicyRule.GATE_UNRECOVERABLE_CAUSE,
            f"Cause '{diagnosis.reason.value}' is on the never-contact list, so no retry and no "
            f"message. The {case.amount_inr:.2f} INR at risk here is recorded as deliberately not "
            "chased, which is the correct outcome rather than a lost opportunity.",
        )

    # 2. A cause we do not recognise. A human decides; the agent does not guess.
    if recoverability is Recoverability.UNKNOWN:
        return build(
            ActionType.ESCALATE_TO_HUMAN,
            PolicyRule.ESCALATE_UNKNOWN_CAUSE,
            f"Failure reason {case.raw_failure_reason!r} is not in the known taxonomy, so no "
            "action can be justified. Routed to a human with the raw gateway text attached. The "
            "agent does not invent a cause in order to have something to do.",
        )

    # 3. No channel exists, so no action could reach the customer anyway.
    if not case.is_contactable:
        return build(
            ActionType.ESCALATE_TO_HUMAN,
            PolicyRule.ESCALATE_NOT_CONTACTABLE,
            "No usable email address or mobile number on this case, so a payment link has nowhere "
            "to go. Escalated for a contact-details fix rather than firing an action into the void "
            "and booking it as an attempt.",
        )

    # 4. Contact budget spent. Permanent, so checked before the temporary stop.
    if case.retry_count >= cap:
        return build(
            ActionType.ESCALATE_TO_HUMAN,
            PolicyRule.ESCALATE_RETRY_CAP_REACHED,
            f"{case.retry_count} of {cap} permitted attempts already used. Further automated "
            "contact would be harassment, so the case goes to a human. Escalation is not a "
            "customer contact, which is why it is still allowed here.",
        )

    # 5. Temporary stop: too soon since the last attempt.
    remaining = cooldown_remaining(case, now=now, cooldown_hours=cooldown_hours)
    if remaining is not None:
        hours = settings.retry_cooldown_hours if cooldown_hours is None else cooldown_hours
        return build(
            ActionType.STOP_NO_ACTION,
            PolicyRule.STOP_COOLDOWN_NOT_ELAPSED,
            f"Last attempt was inside the {hours}h cooldown window; {_fmt_hours(remaining)} still "
            "to run. The money stays recoverable and a later run will pick it up — this is a "
            "'not yet', not a write-off.",
        )

    # 6/7. Cause -> bounded action.
    mapped = ACTION_MAP.get(diagnosis.reason)
    if mapped is not None:
        action, rule = mapped
        if action is ActionType.RETRY_SAME_METHOD:
            reasoning = (
                f"'{diagnosis.reason.value}' is an infrastructure fault, not a decline: nothing is "
                "wrong with the customer's instrument, so re-presenting the same method is fair. "
                f"Attempt {case.retry_count + 1} of {cap}."
            )
        else:
            reasoning = (
                f"'{diagnosis.reason.value}' needs the customer to act, so re-charging the same "
                "instrument would fail identically. Sending a fresh payment link over "
                f"{contact_channel(case)} instead. Attempt {case.retry_count + 1} of {cap}."
            )
        return build(action, rule, reasoning)

    # 8. Recoverable but unmapped. Fail closed.
    return build(
        ActionType.ESCALATE_TO_HUMAN,
        PolicyRule.ESCALATE_NO_ACTION_MAPPED,
        f"Cause '{diagnosis.reason.value}' is classed recoverable but has no entry in the action "
        "map. Failing closed to a human rather than picking the nearest-looking action.",
    )


def decide_all(
    cases: list[FailedPayment],
    diagnoses: list[Diagnosis],
    now: Optional[datetime] = None,
) -> list[Decision]:
    """Decide a whole batch. `cases` and `diagnoses` must be index-aligned."""
    if len(cases) != len(diagnoses):
        raise ValueError(
            f"cases/diagnoses length mismatch ({len(cases)} vs {len(diagnoses)}) — "
            "a case without a diagnosis would be acted on blind"
        )
    now = now or utcnow()
    return [decide(c, d, now=now) for c, d in zip(cases, diagnoses)]


__all__ = [
    "ACTION_MAP",
    "cooldown_remaining",
    "contact_channel",
    "decide",
    "decide_all",
    "effective_recoverability",
    "unmapped_recoverable_reasons",
]
