"""End-to-end tests over the FastAPI surface, using the real generated batch."""

import pytest
from fastapi.testclient import TestClient

from app.main import DATA_PATH, app

client = TestClient(app)

pytestmark = pytest.mark.skipif(
    not DATA_PATH.exists(),
    reason="data/failed_payments.csv missing — run scripts/generate_synthetic_data.py",
)


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_detect_run_returns_cases_and_rejects():
    r = client.get("/detect/run")
    assert r.status_code == 200
    body = r.json()
    assert body["rows_read"] == len(body["cases"]) + len(body["rejected"])
    assert body["cases"], "expected at least one at-risk case"


def test_detect_summary_reconciles():
    body = client.get("/detect/summary").json()["summary"]
    assert (
        body["recoverable_cases"] + body["unrecoverable_cases"] + body["unknown_cases"]
        == body["total_cases"]
    )
    assert (
        body["recoverable_paise"] + body["unrecoverable_paise"] + body["unknown_paise"]
        == body["total_at_risk_paise"]
    )


def test_dirty_rows_are_reported_as_rejects():
    """The generated batch ships deliberate bad rows; they must surface here."""
    rejected = client.get("/detect/rejected").json()
    assert rejected, "expected the injected dirty rows to be rejected"
    reasons = {r["reject_reason"] for r in rejected}
    assert "missing_payment_id" in reasons
    assert "invalid_amount" in reasons
    assert "invalid_timestamp" in reasons
    assert "duplicate_payment_id" in reasons
    for r in rejected:
        assert r["detail"], "every rejection must explain itself"


def test_unrecoverable_endpoint_only_returns_blocked_causes():
    cases = client.get("/detect/unrecoverable").json()
    assert cases, "the batch must contain cases the agent refuses to touch"
    for case in cases:
        assert case["recoverability"] == "unrecoverable"
        assert case["failure_reason"] in {
            "fraud_suspected",
            "card_blocked",
            "customer_cancelled",
        }


def test_no_case_is_both_recoverable_and_blocked():
    cases = client.get("/detect/run").json()["cases"]
    blocked = {"fraud_suspected", "card_blocked", "customer_cancelled"}
    for case in cases:
        if case["failure_reason"] in blocked:
            assert case["recoverability"] == "unrecoverable", case["payment_id"]
        elif case["failure_reason"] != "unknown":
            assert case["recoverability"] == "recoverable", case["payment_id"]


def test_amount_inr_matches_paise_on_every_case():
    for case in client.get("/detect/run").json()["cases"]:
        assert case["amount_inr"] == round(case["amount_paise"] / 100, 2)


def test_payments_summary_matches_detect_summary():
    detected = client.get("/detect/summary").json()["summary"]
    payments = client.get("/payments/summary").json()
    assert payments["total_records"] == detected["total_cases"]
    assert payments["total_amount_at_risk_inr"] == detected["total_at_risk_inr"]


def test_payments_limit_is_respected():
    assert len(client.get("/payments?limit=5").json()) == 5


def test_get_single_payment_roundtrip():
    first = client.get("/payments?limit=1").json()[0]
    r = client.get(f"/payments/{first['payment_id']}")
    assert r.status_code == 200
    assert r.json()["payment_id"] == first["payment_id"]


def test_unknown_payment_id_is_404():
    assert client.get("/payments/pay_does_not_exist").status_code == 404


def test_unknown_cause_case_is_present_and_flagged():
    """The injected unmappable cause must arrive as UNKNOWN, not guessed."""
    cases = client.get("/detect/run").json()["cases"]
    unknown = [c for c in cases if c["failure_reason"] == "unknown"]
    assert unknown, "expected the injected unmappable failure reason"
    for case in unknown:
        assert case["recoverability"] == "unknown"
        assert case["raw_failure_reason"]
        assert case["data_warnings"]
