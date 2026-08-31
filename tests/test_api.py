"""End-to-end tests over the FastAPI surface, using the real generated batch."""

import pytest
from fastapi.testclient import TestClient

from app import main as main_module
from app.main import DATA_PATH, app

client = TestClient(app)

pytestmark = pytest.mark.skipif(
    not DATA_PATH.exists(),
    reason="data/failed_payments.csv missing — run scripts/generate_synthetic_data.py",
)


@pytest.fixture
def dry_run():
    """
    A completed dry run, and the run cache cleared afterwards.

    `app.main._last_run` is process-global, so leaving it populated would make
    later tests pass or fail depending on the order they happened to run in.
    """
    main_module._last_run = None
    response = client.post("/agent/run?dry_run=true&use_llm=false")
    assert response.status_code == 200, response.text
    yield response.json()
    main_module._last_run = None


@pytest.fixture
def no_run():
    main_module._last_run = None
    yield
    main_module._last_run = None


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


# --------------------------------------------------------------------------
# Agent endpoints
# --------------------------------------------------------------------------

def test_health_reports_which_integrations_are_wired():
    body = client.get("/health").json()
    assert "razorpay_configured" in body
    assert "llm_configured" in body
    assert body["policy"]["max_retry_attempts"] >= 1
    assert body["policy"]["retry_cooldown_hours"] >= 1


def test_agent_run_defaults_to_a_dry_run(no_run):
    """
    The safety default, asserted at the HTTP boundary. Nobody should be able to
    contact a customer by clicking Execute in /docs.
    """
    body = client.post("/agent/run?use_llm=false").json()
    assert body["dry_run"] is True
    assert body["metrics"]["customer_contacts_made"] == 0


def test_agent_run_returns_a_full_record_per_case(dry_run):
    assert dry_run["records"], "expected at least one case"
    for record in dry_run["records"]:
        assert record["case"]["payment_id"]
        assert record["diagnosis"]["root_cause"]
        assert record["decision"]["reasoning"]
        assert record["decision"]["policy_rule"]
        assert record["outcome"]["status"]


def test_agent_run_accounts_for_every_row(dry_run):
    assert dry_run["metrics"]["all_invariants_hold"] is True
    assert (
        len(dry_run["records"]) + dry_run["detection"]["rejected_rows"]
        == dry_run["metrics"]["rows_read"]
    )


def test_stopped_endpoint_lists_only_gated_causes(dry_run):
    stopped = client.get("/agent/stopped").json()
    assert stopped, "the batch must contain cases the agent refuses to act on"
    for record in stopped:
        assert record["decision"]["action"] == "stop_no_action"
        assert record["decision"]["contacts_customer"] is False
        assert record["outcome"]["provider_ref"] == ""
        assert record["outcome"]["recovered_paise"] == 0
        assert record["decision"]["policy_rule"] in {
            "gate_unrecoverable_cause",
            "stop_cooldown_not_elapsed",
        }


def test_every_blocked_cause_in_the_batch_is_stopped(dry_run):
    blocked = {"fraud_suspected", "card_blocked", "customer_cancelled"}
    gated = [r for r in dry_run["records"] if r["case"]["failure_reason"] in blocked]
    assert gated, "expected gated causes in the generated batch"
    for record in gated:
        assert record["decision"]["action"] == "stop_no_action"
        assert record["decision"]["policy_rule"] == "gate_unrecoverable_cause"
    assert dry_run["metrics"]["stop_compliance_pct"] == 100.0


def test_escalations_never_contact_the_customer(dry_run):
    for record in client.get("/agent/escalations").json():
        assert record["decision"]["action"] == "escalate_to_human"
        assert record["decision"]["contacts_customer"] is False
        assert record["outcome"]["status"] == "escalated"


def test_decisions_can_be_filtered_by_action(dry_run):
    links = client.get("/agent/decisions?action=send_alt_payment_link").json()
    assert links
    assert all(r["decision"]["action"] == "send_alt_payment_link" for r in links)


def test_an_invalid_action_filter_is_rejected(dry_run):
    assert client.get("/agent/decisions?action=charge_them_twice").status_code == 422


def test_links_endpoint_marks_simulated_refs_as_unverifiable(dry_run):
    links = client.get("/agent/links").json()
    assert links
    for entry in links:
        assert entry["provider_ref"].startswith("dryrun_")
        assert entry["verifiable"] is False


def test_agent_endpoints_409_before_any_run(no_run):
    for path in ["/agent/decisions", "/agent/stopped", "/agent/escalations",
                 "/agent/links", "/metrics", "/metrics/text"]:
        assert client.get(path).status_code == 409, path
    assert client.post("/agent/reconcile").status_code == 409


def test_reconcile_finds_nothing_to_verify_after_a_dry_run(dry_run):
    body = client.post("/agent/reconcile").json()
    assert body["links_checked"] == 0
    assert body["verified_cases"] == 0
    assert "never appear here" in body["note"]


# --------------------------------------------------------------------------
# Metrics endpoint
# --------------------------------------------------------------------------

def test_metrics_keeps_the_two_settlement_columns_apart(dry_run):
    m = client.get("/metrics").json()
    assert "recovered_verified" in m
    assert "recovered_modeled" in m
    assert "recovered_total" not in m
    assert "recovery_rate_pct" not in m
    assert "recovery_rate_verified_pct" in m
    assert "recovery_rate_modeled_pct" in m


def test_metrics_reports_restraint_as_a_headline(dry_run):
    m = client.get("/metrics").json()
    assert m["deliberately_not_chased"]["cases"] > 0
    assert m["deliberately_not_chased"]["amount_inr"] > 0
    assert m["stop_compliance_pct"] == 100.0
    assert m["correctly_stopped_cases"] == m["gated_cases"]


def test_metrics_money_buckets_reconcile(dry_run):
    m = client.get("/metrics").json()
    assert (
        m["recoverable"]["amount_paise"]
        + m["unrecoverable"]["amount_paise"]
        + m["unknown_cause"]["amount_paise"]
        == m["at_risk"]["amount_paise"]
    )
    assert all(m["reconciliation"].values()), m["reconciliation"]


def test_metrics_itemises_the_injected_dirty_rows(dry_run):
    m = client.get("/metrics").json()
    kinds = {e["kind"] for e in m["unresolved_exceptions"]}
    assert "rejected_row" in kinds
    for item in m["unresolved_exceptions"]:
        assert item["reference"]
        assert item["detail"]


def test_metrics_text_is_readable(dry_run):
    text = client.get("/metrics/text").text
    assert "REVENUE RECOVERY" in text
    assert "never added together" in text
    assert "ALL INVARIANTS HOLD: True" in text


# --------------------------------------------------------------------------
# Audit endpoints
# --------------------------------------------------------------------------

def test_audit_trail_verifies_after_a_run(dry_run):
    body = client.get("/audit/verify").json()
    assert body["ok"] is True
    assert body["events"] > 0


def test_audit_lists_the_run(dry_run):
    runs = client.get("/audit/runs").json()["runs"]
    assert dry_run["run_id"] in runs


def test_audit_can_be_filtered_to_one_case(dry_run):
    payment_id = dry_run["records"][0]["case"]["payment_id"]
    events = client.get(f"/audit?payment_id={payment_id}").json()
    stages = [e["stage"] for e in events]
    assert {"case_detected", "diagnosed", "decided", "acted"} <= set(stages)
    # The decision is recorded before the action it authorises.
    assert stages.index("decided") < stages.index("acted")


def test_audit_can_be_filtered_by_stage(dry_run):
    events = client.get("/audit?stage=decided&limit=500").json()
    assert events
    assert all(e["stage"] == "decided" for e in events)


def test_every_audit_entry_explains_itself_and_is_chained(dry_run):
    events = client.get("/audit?limit=500").json()
    assert events
    for event in events:
        assert event["summary"].strip()
        assert len(event["entry_hash"]) == 64
        assert event["prev_hash"]


def test_an_invalid_audit_stage_is_rejected(dry_run):
    assert client.get("/audit?stage=took_the_money_and_ran").status_code == 422
