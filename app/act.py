"""
Act stage: execute one decision against Razorpay test mode, and account for what
came back.

Only two of the four permitted actions touch the network. `STOP_NO_ACTION` and
`ESCALATE_TO_HUMAN` are deliberately implemented as *nothing happening* — no
call, no message, no queue write. That is the point of them.

    RETRY_SAME_METHOD      -> client.order.create      (real, test mode)
    SEND_ALT_PAYMENT_LINK  -> client.payment_link.create (real, test mode)
    ESCALATE_TO_HUMAN      -> record only
    STOP_NO_ACTION         -> record only


Where recovered rupees come from
-------------------------------

Two sources, kept apart everywhere:

* **VERIFIED_API** — `reconcile()` asked Razorpay whether a link was paid and
  Razorpay said yes. This is the only path to a verified rupee. It requires a
  human to actually pay a test-mode link with a test card, which is how a handful
  of cases in this build are proven end to end.

* **MODELED** — `apply_modeled_settlement()` resolves the rest with the seeded
  model below. Reported in its own column, never added to the verified one.

A consequence worth stating rather than hiding: a `RETRY_SAME_METHOD` outcome can
**only ever be modeled**. Nobody can pay an Order by hand, so no retry rupee is
ever verifiable in this build. Only payment links can be verified.

The probabilities in `BASE_RECOVERY_PROBABILITY` are *stated assumptions*, not
measurements — a card that has already expired rarely converts, a bank that timed
out usually clears on a second pass. They are plausible, and that is the strongest
claim available. Labelling this column MODELED and refusing to sum it with the
verified column is the honest way to use a number nobody has measured.
"""

from __future__ import annotations

import hashlib
from typing import Optional

from app.config import settings
from app.models import (
    ActionOutcome,
    ActionStatus,
    ActionType,
    Decision,
    FailedPayment,
    FailureReason,
    SettlementSource,
)
from app.services import razorpay_client

#: Assumed probability that a recovery attempt on this cause eventually settles.
#: Assumption, not measurement — see the module docstring.
BASE_RECOVERY_PROBABILITY: dict[FailureReason, float] = {
    FailureReason.BANK_TIMEOUT: 0.71,
    FailureReason.NETWORK_ERROR: 0.69,
    FailureReason.GATEWAY_ERROR: 0.66,
    FailureReason.ISSUER_UNAVAILABLE: 0.58,
    FailureReason.INVALID_OTP: 0.52,
    FailureReason.INSUFFICIENT_FUNDS: 0.34,
    FailureReason.CARD_EXPIRED: 0.28,
}

#: Each prior failed attempt makes the next one less likely. Without this the
#: model would imply a case gets no harder to recover the more it has already
#: refused, which would flatter the retry-heavy end of the batch.
ATTEMPT_DECAY = 0.75


def deterministic_draw(payment_id: str, seed: Optional[int] = None) -> float:
    """
    A stable uniform draw in [0, 1) for one case.

    sha256 rather than `hash()`: Python randomises string hashing per process
    unless PYTHONHASHSEED is pinned, so `hash()` would make the modeled column
    change between runs of the same batch. A metric that moves when nothing moved
    is not a metric.
    """
    seed = settings.outcome_model_seed if seed is None else seed
    digest = hashlib.sha256(f"{seed}:{payment_id}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def modeled_probability(case: FailedPayment, reason: FailureReason) -> float:
    base = BASE_RECOVERY_PROBABILITY.get(reason, 0.0)
    return round(base * (ATTEMPT_DECAY ** case.retry_count), 4)


def link_description(case: FailedPayment) -> str:
    """Customer-facing text. No internal jargon, no failure-reason codes — the
    customer does not need to be told our risk engine's opinion of them."""
    ref = case.order_id or case.payment_id
    return f"Complete your payment of INR {case.amount_inr:,.2f} for order {ref}"


def _notes(case: FailedPayment, decision: Decision, run_id: str) -> dict:
    """Metadata attached to the Razorpay object so a reviewer looking at the
    dashboard can trace any artefact back to the decision that created it."""
    return {
        "payment_id": case.payment_id,
        "order_id": case.order_id,
        "customer_id": case.customer_id,
        "failure_reason": decision.reason.value,
        "policy_rule": decision.policy_rule.value,
        "agent_run_id": run_id,
    }


def execute(
    case: FailedPayment,
    decision: Decision,
    run_id: str,
    dry_run: bool = True,
) -> ActionOutcome:
    """
    Carry out one decision.

    Never raises. A Razorpay failure becomes `ActionStatus.FAILED` with the error
    text preserved, because a single 500 must not abort a batch and must not
    disappear either — failed calls are itemised in the metrics report as
    unresolved exceptions.
    """
    base = dict(payment_id=case.payment_id, action=decision.action)

    if decision.action is ActionType.STOP_NO_ACTION:
        return ActionOutcome(
            **base,
            status=ActionStatus.NO_ACTION,
            detail=f"No action taken. Rule: {decision.policy_rule.value}.",
            settlement_source=SettlementSource.NONE,
        )

    if decision.action is ActionType.ESCALATE_TO_HUMAN:
        return ActionOutcome(
            **base,
            status=ActionStatus.ESCALATED,
            detail=(
                f"Queued for human review. Rule: {decision.policy_rule.value}. "
                "No customer contact was made."
            ),
            settlement_source=SettlementSource.NONE,
        )

    if not dry_run and not razorpay_client.is_configured():
        return ActionOutcome(
            **base,
            status=ActionStatus.FAILED,
            error="Razorpay test keys are not configured; live action refused",
            detail="Set RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET, or run with dry_run=true.",
            settlement_source=SettlementSource.NONE,
        )

    reference_id = f"{case.payment_id}-{run_id[:8]}"

    if decision.action is ActionType.SEND_ALT_PAYMENT_LINK:
        if dry_run:
            return ActionOutcome(
                **base,
                status=ActionStatus.SIMULATED,
                provider_object="payment_link",
                provider_ref=f"dryrun_plink_{case.payment_id[-10:]}",
                detail=(
                    f"DRY RUN: would create a payment link for INR {case.amount_inr:,.2f} "
                    f"notified over {decision.contact_channel}. Nothing left this process."
                ),
                settlement_source=SettlementSource.NONE,
            )
        try:
            link = razorpay_client.create_payment_link(
                amount_paise=case.amount_paise,
                customer_name=case.customer_name,
                customer_email=case.customer_email if case.has_valid_email else "",
                customer_phone=case.customer_phone if case.has_valid_phone else "",
                description=link_description(case),
                reference_id=reference_id,
                notes=_notes(case, decision, run_id),
            )
        except Exception as exc:
            return ActionOutcome(
                **base,
                status=ActionStatus.FAILED,
                provider_object="payment_link",
                error=f"{type(exc).__name__}: {exc}"[:500],
                detail="Payment link creation failed; case left untouched for the next run.",
                settlement_source=SettlementSource.NONE,
            )
        return ActionOutcome(
            **base,
            status=ActionStatus.EXECUTED,
            provider_object="payment_link",
            provider_ref=str(link.get("id", "")),
            provider_short_url=str(link.get("short_url", "")),
            detail=(
                f"Razorpay test-mode payment link created for INR {case.amount_inr:,.2f}, "
                f"notified over {decision.contact_channel}."
            ),
            settlement_source=SettlementSource.NONE,
        )

    if decision.action is ActionType.RETRY_SAME_METHOD:
        if dry_run:
            return ActionOutcome(
                **base,
                status=ActionStatus.SIMULATED,
                provider_object="order",
                provider_ref=f"dryrun_order_{case.payment_id[-10:]}",
                detail=(
                    f"DRY RUN: would create a re-presentment order for INR "
                    f"{case.amount_inr:,.2f}. Nothing left this process."
                ),
                settlement_source=SettlementSource.NONE,
            )
        try:
            order = razorpay_client.create_order(
                amount_paise=case.amount_paise,
                receipt=reference_id,
                notes=_notes(case, decision, run_id),
            )
        except Exception as exc:
            return ActionOutcome(
                **base,
                status=ActionStatus.FAILED,
                provider_object="order",
                error=f"{type(exc).__name__}: {exc}"[:500],
                detail="Order creation failed; case left untouched for the next run.",
                settlement_source=SettlementSource.NONE,
            )
        return ActionOutcome(
            **base,
            status=ActionStatus.EXECUTED,
            provider_object="order",
            provider_ref=str(order.get("id", "")),
            detail=(
                f"Razorpay test-mode order created as the re-presentment of INR "
                f"{case.amount_inr:,.2f}. An order cannot be paid by hand, so this case's "
                "settlement can only ever be modeled, never verified."
            ),
            settlement_source=SettlementSource.NONE,
        )

    # Unreachable while ActionType stays closed, but fail loudly if it doesn't.
    return ActionOutcome(
        **base,
        status=ActionStatus.FAILED,
        error=f"No executor for action {decision.action.value}",
        detail="ActionType gained a member without a bounded implementation.",
        settlement_source=SettlementSource.NONE,
    )


def apply_modeled_settlement(
    case: FailedPayment,
    decision: Decision,
    outcome: ActionOutcome,
    seed: Optional[int] = None,
) -> ActionOutcome:
    """
    Resolve an attempted case through the seeded model.

    Refuses to touch anything already VERIFIED_API — a real API confirmation
    outranks a model, always. Cases that were stopped, escalated or failed are
    left at zero: the agent gets no credit for money it never chased.
    """
    if outcome.settlement_source is SettlementSource.VERIFIED_API:
        return outcome
    if outcome.status not in {ActionStatus.EXECUTED, ActionStatus.SIMULATED}:
        return outcome

    probability = modeled_probability(case, decision.reason)
    draw = deterministic_draw(case.payment_id, seed=seed)
    settled = draw < probability

    detail = (
        f"Modeled outcome: p={probability:.2f} for '{decision.reason.value}' after "
        f"{case.retry_count} prior attempt(s); seeded draw={draw:.4f} -> "
        f"{'settled' if settled else 'not settled'}."
    )
    return outcome.model_copy(
        update={
            "recovered_paise": case.amount_paise if settled else 0,
            "settlement_source": SettlementSource.MODELED if settled else SettlementSource.NONE,
            "settlement_detail": detail,
        }
    )


def reconcile(outcome: ActionOutcome) -> ActionOutcome:
    """
    Ask Razorpay whether a previously-created link was actually paid, and upgrade
    the outcome to VERIFIED_API if so.

    One-way: this can promote a case from modeled to verified, never demote a
    verified one. Called on a *later* run than the one that created the link,
    because the human paying the test card needs to happen in between.
    """
    if outcome.provider_object != "payment_link" or not outcome.provider_ref:
        return outcome
    if outcome.provider_ref.startswith("dryrun_"):
        return outcome.model_copy(
            update={"settlement_detail": "Not reconciled: link was simulated, not created."}
        )
    if not razorpay_client.is_configured():
        return outcome.model_copy(
            update={"settlement_detail": "Not reconciled: Razorpay keys are not configured."}
        )

    try:
        status = razorpay_client.fetch_payment_link_status(outcome.provider_ref)
    except Exception as exc:
        return outcome.model_copy(
            update={"settlement_detail": f"Reconciliation failed: {type(exc).__name__}: {exc}"[:400]}
        )

    state = str(status.get("status", "")).lower()
    amount_paid = int(status.get("amount_paid") or 0)

    if state == "paid" and amount_paid > 0:
        return outcome.model_copy(
            update={
                "recovered_paise": amount_paid,
                "settlement_source": SettlementSource.VERIFIED_API,
                "settlement_detail": (
                    f"VERIFIED: Razorpay reports link {outcome.provider_ref} as paid, "
                    f"amount_paid={amount_paid} paise."
                ),
            }
        )

    # Not paid. Leave any modeled figure exactly as it was and say what we saw.
    return outcome.model_copy(
        update={
            "settlement_detail": (
                f"{outcome.settlement_detail} Reconciled against Razorpay: link is "
                f"'{state or 'unknown'}', amount_paid={amount_paid} paise."
            ).strip()
        }
    )


__all__ = [
    "ATTEMPT_DECAY",
    "BASE_RECOVERY_PROBABILITY",
    "apply_modeled_settlement",
    "deterministic_draw",
    "execute",
    "link_description",
    "modeled_probability",
    "reconcile",
]
