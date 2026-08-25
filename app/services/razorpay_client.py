"""
Thin wrapper around the Razorpay Python SDK, scoped to TEST MODE only.

Every function here is a real, bounded action the agent is allowed to take.
Keep this list explicit — the buildathon rubric specifically wants every
money-moving action to be "explainable, bounded and gated". If the agent
needs a new capability, add a new function here rather than letting it
call the SDK directly.
"""

import razorpay

from app.config import settings

_client: razorpay.Client | None = None


def get_client() -> razorpay.Client:
    global _client
    if _client is None:
        if not settings.razorpay_key_id or not settings.razorpay_key_secret:
            raise RuntimeError(
                "Razorpay test keys are not set. Add RAZORPAY_KEY_ID and "
                "RAZORPAY_KEY_SECRET to your .env file (see .env.example)."
            )
        _client = razorpay.Client(auth=(settings.razorpay_key_id, settings.razorpay_key_secret))
    return _client


def create_payment_link(amount_paise: int, customer_name: str, customer_email: str,
                        customer_phone: str, description: str,
                        reference_id: str | None = None,
                        notes: dict | None = None) -> dict:
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
        "notify": {"sms": True, "email": True},
        # Razorpay's own reminders are left ON for the link, but the agent's
        # contact policy (attempt caps, cooldowns) is enforced upstream in the
        # decide stage — that's where "don't harass the customer" is guaranteed.
        "reminder_enable": True,
    }
    if reference_id:
        # Lets us tie a link back to the originating payment for the audit trail.
        payload["reference_id"] = reference_id
    if notes:
        payload["notes"] = notes
    return client.payment_link.create(payload)


def fetch_payment(payment_id: str) -> dict:
    """Look up the current status of a payment in test mode."""
    client = get_client()
    return client.payment.fetch(payment_id)


def fetch_payment_link_status(plink_id: str) -> dict:
    """Check whether a previously-created payment link has been paid."""
    client = get_client()
    return client.payment_link.fetch(plink_id)
