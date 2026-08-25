"""
Generates a synthetic batch of failed-payment records for the recovery agent.

    python scripts/generate_synthetic_data.py                  # 80 clean + 8 dirty, seed 42
    python scripts/generate_synthetic_data.py --count 200
    python scripts/generate_synthetic_data.py --seed 7 --dirty 0

Output: data/failed_payments.csv

Two things this script deliberately does:

1. **It is seeded.** The batch is byte-identical across runs for a given seed, so
   the ₹ figures quoted in the README, the metrics report, and the pitch video
   all refer to the same batch. An unreproducible headline number is not a
   measurement.

2. **It injects dirty rows.** Real recovery data has duplicates, junk amounts,
   unparseable dates and causes nobody mapped. The agent has to survive them and
   report them — see the DIRTY_ROW_BUILDERS section for exactly what each one is
   supposed to prove.

The mix of recoverable and unrecoverable causes is what lets the agent
demonstrate its stopping rule: every fraud_suspected / card_blocked /
customer_cancelled case must come out the far end untouched.
"""

from __future__ import annotations

import argparse
import csv
import random
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

FIRST_NAMES = [
    "Aarav", "Vivaan", "Aditi", "Diya", "Kabir", "Meera", "Rohan", "Sanya",
    "Arjun", "Priya", "Karan", "Ishita", "Nikhil", "Ananya", "Rahul", "Neha",
]
LAST_NAMES = [
    "Sharma", "Verma", "Iyer", "Reddy", "Nair", "Gupta", "Menon", "Rao",
    "Kapoor", "Singh", "Das", "Pillai",
]

# reason -> (weight, typical error_description, recoverable)
REASONS: dict[str, tuple[int, str, bool]] = {
    "insufficient_funds": (18, "Payment failed due to insufficient balance in account", True),
    "bank_timeout": (14, "Bank server did not respond within timeout window", True),
    "gateway_error": (12, "Payment gateway returned a temporary processing error", True),
    "invalid_otp": (10, "Customer entered incorrect OTP, payment authorization failed", True),
    "card_expired": (10, "Card used for payment has expired", True),
    "network_error": (8, "Network interruption during payment authorization", True),
    "issuer_unavailable": (8, "Card issuing bank system temporarily unavailable", True),
    "fraud_suspected": (8, "Payment blocked by risk engine - suspected fraudulent activity", False),
    "card_blocked": (7, "Card has been blocked by the issuing bank", False),
    "customer_cancelled": (5, "Customer cancelled the payment during checkout", False),
}

FIELDNAMES = [
    "payment_id", "order_id", "customer_id", "customer_name", "customer_email",
    "customer_phone", "amount_inr", "currency", "failure_reason",
    "error_description", "created_at", "retry_count", "last_attempt_at",
]

AMOUNT_TIERS = [299, 499, 999, 1499, 2499, 4999, 9999, 14999]


def gen_clean_record(rng: random.Random, i: int, now: datetime) -> dict:
    reason = rng.choices(
        list(REASONS.keys()), weights=[v[0] for v in REASONS.values()], k=1
    )[0]
    description = REASONS[reason][1]

    first, last = rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)
    created = now - timedelta(hours=rng.randint(1, 96), minutes=rng.randint(0, 59))

    # Some records already carry prior attempts so the cooldown / retry-cap
    # rules have something to bite on.
    retry_count = rng.choices([0, 1, 2, 3], weights=[60, 20, 12, 8], k=1)[0]

    # A prior attempt must sit between creation and now. The earlier version of
    # this script could place it in the future, which is impossible and would
    # have produced negative cooldowns downstream.
    last_attempt = ""
    if retry_count > 0:
        window_seconds = int((now - created).total_seconds())
        if window_seconds > 60:
            offset = rng.randint(60, window_seconds)
            last_attempt = (created + timedelta(seconds=offset)).isoformat()

    amount = round(rng.choice(AMOUNT_TIERS) * rng.uniform(0.9, 1.1), 2)

    return {
        "payment_id": f"pay_{uuid.UUID(int=rng.getrandbits(128)).hex[:14]}",
        "order_id": f"order_{uuid.UUID(int=rng.getrandbits(128)).hex[:14]}",
        "customer_id": f"cust_{uuid.UUID(int=rng.getrandbits(128)).hex[:10]}",
        "customer_name": f"{first} {last}",
        "customer_email": f"{first.lower()}.{last.lower()}{i}@example.com",
        "customer_phone": f"9{rng.randint(100000000, 999999999)}",
        "amount_inr": amount,
        "currency": "INR",
        "failure_reason": reason,
        "error_description": description,
        "created_at": created.isoformat(),
        "retry_count": retry_count,
        "last_attempt_at": last_attempt,
    }


# Each builder returns (row, what_this_row_proves). Order is stable so the
# dirty set is reproducible for a given --dirty count.
def _dirty_missing_payment_id(rng, now, clean):
    row = gen_clean_record(rng, 900, now)
    row["payment_id"] = ""
    return row, "REJECTED missing_payment_id — a case with no id cannot be tracked or audited"


def _dirty_negative_amount(rng, now, clean):
    row = gen_clean_record(rng, 901, now)
    row["amount_inr"] = "-1499.00"
    return row, "REJECTED invalid_amount — refuse to act on money we don't trust"


def _dirty_unparseable_amount(rng, now, clean):
    row = gen_clean_record(rng, 902, now)
    row["amount_inr"] = "N/A"
    return row, "REJECTED invalid_amount — garbage in the money column"


def _dirty_bad_timestamp(rng, now, clean):
    row = gen_clean_record(rng, 903, now)
    row["created_at"] = "not-a-date"
    return row, "REJECTED invalid_timestamp — cooldown arithmetic is impossible"


def _dirty_duplicate(rng, now, clean):
    row = gen_clean_record(rng, 904, now)
    if clean:
        row["payment_id"] = clean[0]["payment_id"]
    return row, "REJECTED duplicate_payment_id — prevents contacting one customer twice"


def _dirty_unknown_reason(rng, now, clean):
    row = gen_clean_record(rng, 905, now)
    row["failure_reason"] = "quantum_flux_declined"
    row["error_description"] = "Unrecognised processor response code 0x5B"
    return row, "ACCEPTED but UNKNOWN cause — must escalate to a human, never guess an action"


def _dirty_uncontactable(rng, now, clean):
    row = gen_clean_record(rng, 906, now)
    row["customer_email"] = "not-an-email"
    row["customer_phone"] = "12345"
    return row, "ACCEPTED but not contactable — no channel exists, so escalate rather than 'send link'"


def _dirty_reason_alias(rng, now, clean):
    row = gen_clean_record(rng, 907, now)
    row["failure_reason"] = "Insufficient Balance"
    row["error_description"] = "Account balance too low to complete transaction"
    return row, "ACCEPTED via alias mapping — 'Insufficient Balance' normalizes to insufficient_funds"


DIRTY_ROW_BUILDERS = [
    _dirty_missing_payment_id,
    _dirty_negative_amount,
    _dirty_bad_timestamp,
    _dirty_duplicate,
    _dirty_unknown_reason,
    _dirty_uncontactable,
    _dirty_unparseable_amount,
    _dirty_reason_alias,
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=80, help="clean records to generate")
    parser.add_argument(
        "--dirty",
        type=int,
        default=len(DIRTY_ROW_BUILDERS),
        help=f"dirty records to inject (0-{len(DIRTY_ROW_BUILDERS)})",
    )
    parser.add_argument("--seed", type=int, default=42, help="RNG seed for reproducibility")
    parser.add_argument("--out", type=Path, default=None, help="output CSV path")
    args = parser.parse_args()

    n_dirty = max(0, min(args.dirty, len(DIRTY_ROW_BUILDERS)))
    rng = random.Random(args.seed)

    # Fixed reference time so a given seed yields identical relative timestamps.
    # (Absolute dates shift with the run date; the *shape* of the batch does not.)
    now = datetime.now(timezone.utc).replace(microsecond=0)

    clean = [gen_clean_record(rng, i, now) for i in range(args.count)]

    dirty_rows: list[dict] = []
    notes: list[str] = []
    for builder in DIRTY_ROW_BUILDERS[:n_dirty]:
        row, note = builder(rng, now, clean)
        dirty_rows.append(row)
        notes.append(note)

    records = clean + dirty_rows
    rng.shuffle(records)

    out_path = args.out or (Path(__file__).resolve().parent.parent / "data" / "failed_payments.csv")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(records)

    recoverable = sum(1 for r in clean if REASONS[r["failure_reason"]][2])
    unrecoverable = len(clean) - recoverable

    print(f"Wrote {len(records)} rows to {out_path}  (seed={args.seed})")
    print(f"  clean rows:        {len(clean)}")
    print(f"    recoverable:     {recoverable}")
    print(f"    unrecoverable:   {unrecoverable}   <- stopping logic must catch every one")
    print(f"  dirty rows:        {len(dirty_rows)}")
    for note in notes:
        print(f"    - {note}")


if __name__ == "__main__":
    main()
