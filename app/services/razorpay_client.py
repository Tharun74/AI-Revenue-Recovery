"""
Thin wrapper around the Razorpay Python SDK, scoped to TEST MODE only.

Every function here is a real, bounded action the agent is allowed to take.
Keep this list explicit — the buildathon rubric specifically wants every
money-moving action to be "explainable, bounded and gated". If the agent
needs a new capability, add a new function here rather than letting it
call the SDK directly.

The complete list of things this process can ask Razorpay to do:

    create_payment_link      the primary recovery action
    create_order             the re-presentment recorded for a retry
    fetch_payment            read a payment's status
    fetch_payment_link       read a link's status (this is how ₹ get *verified*)

There is no capture, no refund, no transfer and no customer-facing messaging
beyond the link's own notification. Nothing here can move money without the
customer completing a payment themselves.
"""

from __future__ import annotations

import time

import razorpay

from app.config import settings

_client: razorpay.Client | None = None

#: How long a recovery link stays payable. A link that never expires is an
#: unbounded action with a slow fuse.
LINK_TTL_SECONDS = 7 * 24 * 3600


def is_configured() -> bool:
    """Whether real test-mode calls are possible. Checked before acting so a
    missing key becomes a reported exception, not a stack trace mid-batch."""
    return bool(settings.razorpay_key_id and settings.razorpay_key_secret)


def get_client() -> razorpay.Client:
    global _client
    if _client is None:
        if not is_configured():
            raise RuntimeError(
                "Razorpay test keys are not set. Add RAZORPAY_KEY_ID and "
                "RAZORPAY_KEY_SECRET to your .env file (see .env.example)."
            )
        if not settings.razorpay_key_id.startswith("rzp_test"):
            # Hard refusal rather than a warning. This project only ever has
            # cause to talk to test mode, and a live key here would mean real
            # customers receiving real payment requests from a demo.
            raise RuntimeError(
                f"RAZORPAY_KEY_ID {settings.razorpay_key_id[:12]!r} is not a test-mode key. "
                "This agent refuses to run against live credentials."
            )
        _client = razorpay.Client(auth=(settings.razorpay_key_id, settings.razorpay_key_secret))
    return _client


def reset_client() -> None:
    """Drop the cached client. Used by tests that swap credentials."""
    global _client
    _client = None


def _stringify_notes(notes: dict | None) -> dict:
    """Razorpay rejects non-string note values, and silently losing audit
    metadata to a 400 is not acceptable."""
    return {str(k): str(v) for k, v in (notes or {}).items()}


def create_payment_link(amount_paise: int, customer_name: str, customer_email: str,
                        customer_phone: str, description: str,
                        reference_id: str | None = None,
                        notes: dict | None = None,
                        expire_after_seconds: int | None = None) -> dict:
    """
    Creates a Razorpay test-mode Payment Link the customer can use to complete
    payment. This is the agent's primary recovery action.

    `amount_paise` is an integer on purpose — it's the canonical money value
    carried by FailedPayment, and Razorpay's API speaks paise natively. Taking
    rupees as a float here would reintroduce rounding drift at the last step.
    """
    client = get_client()
    payload: dict = {
        "amount": amount_paise,
        "currency": "INR",
        "description": description,
        "customer": {
            "name": customer_name,
            "email": customer_email,
            "contact": customer_phone,
        },
        "notify": {"sms": bool(customer_phone), "email": bool(customer_email)},
        # Razorpay's own reminders are left ON for the link, but the agent's
        # contact policy (attempt caps, cooldowns) is enforced upstream in the
        # decide stage — that's where "don't harass the customer" is guaranteed.
        "reminder_enable": True,
        "expire_by": int(time.time()) + (expire_after_seconds or LINK_TTL_SECONDS),
    }
    if reference_id:
        # Lets us tie a link back to the originating payment for the audit trail.
        # Razorpay enforces uniqueness here, so callers suffix it with the run id.
        payload["reference_id"] = reference_id
    if notes:
        payload["notes"] = _stringify_notes(notes)
    return client.payment_link.create(payload)


def create_order(amount_paise: int, receipt: str | None = None,
                 notes: dict | None = None) -> dict:
    """
    Creates a test-mode Order representing a re-presentment of the original
    charge — the concrete artefact behind ActionType.RETRY_SAME_METHOD.

    Why an Order and not an actual re-charge: silently charging a card again
    requires a stored token or mandate, and the synthetic batch has neither. So
    this records a real, auditable re-attempt against Razorpay without
    pretending a charge was authorised that never was. The consequence is stated
    plainly in the report — a retry's settlement can only ever be MODELED, never
    VERIFIED_API, because nobody can pay an Order by hand.
    """
    client = get_client()
    payload: dict = {
        "amount": amount_paise,
        "currency": "INR",
        "payment_capture": 1,
    }
    if receipt:
        payload["receipt"] = receipt[:40]  # Razorpay caps receipt length
    if notes:
        payload["notes"] = _stringify_notes(notes)
    return client.order.create(payload)


def fetch_payment(payment_id: str) -> dict:
    """Look up the current status of a payment in test mode."""
    client = get_client()
    return client.payment.fetch(payment_id)


def fetch_payment_link_status(plink_id: str) -> dict:
    """
    Check whether a previously-created payment link has been paid. This call is
    the sole source of SettlementSource.VERIFIED_API — every verified rupee in
    the report traces back to a response from here.
    """
    client = get_client()
    return client.payment_link.fetch(plink_id)
