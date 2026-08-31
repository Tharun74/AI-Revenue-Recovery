"""
Run the full recovery workflow from the command line and print the batch report.

    python scripts/run_batch.py                    # dry run, no external calls
    python scripts/run_batch.py --live             # real Razorpay test-mode calls
    python scripts/run_batch.py --live --limit 5   # a handful of real links
    python scripts/run_batch.py --no-llm           # deterministic diagnosis only
    python scripts/run_batch.py --reconcile RUN_ID # check which links got paid
    python scripts/run_batch.py --verify-audit     # re-walk the audit hash chain

`--live` is opt-in and prints what it is about to do before doing it. Everything in
this project defaults to touching nobody.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent import reconcile_run, run_batch  # noqa: E402
from app.audit import default_trail  # noqa: E402
from app.config import settings  # noqa: E402
from app.metrics import render_text_report  # noqa: E402
from app.services import llm_client, razorpay_client  # noqa: E402


def cmd_verify_audit() -> int:
    result = default_trail.verify()
    print(f"audit trail: {result.path}")
    print(f"  entries: {result.events}")
    print(f"  intact:  {result.ok}")
    print(f"  detail:  {result.detail}")
    if not result.ok:
        print(f"  broken at seq: {result.broken_at_seq}")
    return 0 if result.ok else 1


def cmd_reconcile(run_id: str) -> int:
    result = reconcile_run(run_id)
    print(f"Reconciled run {run_id}")
    print(f"  links checked:  {result['links_checked']}")
    print(f"  verified cases: {result['verified_cases']}")
    print(f"  verified INR:   {result['verified_inr']:,.2f}")
    print(f"  note: {result['note']}")
    for entry in result["links"]:
        mark = "VERIFIED" if entry.get("verified") else "        "
        print(f"  [{mark}] {entry.get('payment_id','')} {entry.get('provider_ref','')} "
              f"{entry.get('short_url','')}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--live", action="store_true",
                        help="make real Razorpay test-mode calls (default is a dry run)")
    parser.add_argument("--no-llm", action="store_true",
                        help="skip the LLM and diagnose with rules only")
    parser.add_argument("--no-settle", action="store_true",
                        help="skip the seeded outcome model; report decisions only")
    parser.add_argument("--limit", type=int, default=None,
                        help="process only the first N cases")
    parser.add_argument("--json", action="store_true",
                        help="print the report as JSON instead of text")
    parser.add_argument("--reconcile", metavar="RUN_ID",
                        help="re-check a past run's payment links and exit")
    parser.add_argument("--verify-audit", action="store_true",
                        help="re-walk the audit hash chain and exit")
    args = parser.parse_args()

    if args.verify_audit:
        return cmd_verify_audit()
    if args.reconcile:
        return cmd_reconcile(args.reconcile)

    if args.live:
        if not razorpay_client.is_configured():
            print("ERROR: --live needs RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET in .env",
                  file=sys.stderr)
            return 2
        scope = f"the first {args.limit} cases" if args.limit else "the whole batch"
        print(f"LIVE MODE: creating real Razorpay TEST-MODE payment links and orders for {scope}.")
        print("           Nothing here can move real money, but real API objects will exist.")
        print()

    if not args.no_llm and not llm_client.is_available():
        print(f"NOTE: diagnosis will use the rule-based path ({llm_client.unavailable_reason()}).")
        print()

    run = run_batch(
        dry_run=not args.live,
        use_llm=not args.no_llm,
        limit=args.limit,
        settle=not args.no_settle,
    )

    if args.json:
        print(run.metrics.model_dump_json(indent=2))
    else:
        print(render_text_report(run.metrics))
        print()
        print(f"audit trail: {settings.audit_log_path}")
        print(f"  entries this run: {len(default_trail.query(run_id=run.run_id))}")
        print(f"  chain intact:     {default_trail.verify().ok}")
        if args.live:
            links = [
                r for r in run.records
                if r.outcome.provider_object == "payment_link" and r.outcome.provider_short_url
            ]
            if links:
                print()
                print("PAYABLE TEST LINKS — pay one with a Razorpay test card, then run:")
                print(f"  python scripts/run_batch.py --reconcile {run.run_id}")
                for record in links[:10]:
                    print(f"  INR {record.case.amount_inr:>10,.2f}  {record.outcome.provider_short_url}")

    # A run whose own arithmetic does not reconcile is a failed run.
    return 0 if run.metrics.all_invariants_hold else 1


if __name__ == "__main__":
    raise SystemExit(main())
