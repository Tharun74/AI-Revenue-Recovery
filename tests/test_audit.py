"""
Tests for the append-only audit trail.

The tamper tests are the point of the module: `test_editing_an_entry_breaks_the_chain`,
`test_deleting_an_entry_breaks_the_chain` and `test_reordering_entries_breaks_the_chain`.
An audit trail nobody can check is decoration, so the checks are what get tested
hardest.
"""

import json
from datetime import datetime, timezone

import pytest

from app.audit import GENESIS_HASH, AuditTrail, compute_hash
from app.config import settings
from app.models import AuditStage

AT = datetime(2026, 8, 25, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def log(tmp_path):
    return AuditTrail(tmp_path / "audit.jsonl")


def fill(trail, n=5, run_id="run_test"):
    return [
        trail.append(
            run_id=run_id,
            stage=AuditStage.DECIDED,
            payment_id=f"pay_{i}",
            summary=f"decision {i}",
            payload={"index": i},
            at=AT,
        )
        for i in range(n)
    ]


def rewrite(path, records):
    path.write_text(
        "\n".join(json.dumps(r, sort_keys=True, separators=(",", ":")) for r in records) + "\n",
        encoding="utf-8",
    )


def read_records(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# --------------------------------------------------------------------------
# Writing and reading
# --------------------------------------------------------------------------

def test_first_entry_chains_from_genesis(log):
    event = log.append(run_id="r", stage=AuditStage.RUN_STARTED, summary="go", at=AT)
    assert event.seq == 1
    assert event.prev_hash == GENESIS_HASH
    assert len(event.entry_hash) == 64


def test_sequence_numbers_are_dense_and_increasing(log):
    events = fill(log, 10)
    assert [e.seq for e in events] == list(range(1, 11))


def test_each_entry_points_at_the_previous_one(log):
    events = fill(log, 6)
    for previous, current in zip(events, events[1:]):
        assert current.prev_hash == previous.entry_hash


def test_the_file_is_one_json_object_per_line(log):
    fill(log, 4)
    lines = log.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 4
    for line in lines:
        assert json.loads(line)["stage"] == "decided"


def test_events_round_trip_through_the_file(log):
    fill(log, 3)
    events = log.read_all()
    assert [e.summary for e in events] == ["decision 0", "decision 1", "decision 2"]
    assert events[0].payload == {"index": 0}
    assert events[0].stage is AuditStage.DECIDED


def test_pydantic_payloads_are_serialised(log):
    from app.models import FailureReason, PolicyRule, Recoverability
    from app.models import Decision

    decision = Decision(
        payment_id="pay_x", amount_paise=149900, reason=FailureReason.CARD_EXPIRED,
        recoverability=Recoverability.RECOVERABLE, action="send_alt_payment_link",
        policy_rule=PolicyRule.LINK_CUSTOMER_ACTION_REQUIRED, reasoning="because",
    )
    log.append(run_id="r", stage=AuditStage.DECIDED, payload=decision, at=AT)

    stored = log.read_all()[0].payload
    assert stored["payment_id"] == "pay_x"
    assert stored["reason"] == "card_expired"
    assert stored["amount_inr"] == 1499.00  # computed fields survive


def test_a_reopened_trail_continues_the_chain(log, tmp_path):
    first = fill(log, 3)
    reopened = AuditTrail(tmp_path / "audit.jsonl")
    event = reopened.append(run_id="r2", stage=AuditStage.RUN_COMPLETED, summary="done", at=AT)
    assert event.seq == 4
    assert event.prev_hash == first[-1].entry_hash
    assert reopened.verify().ok


def test_appending_never_rewrites_earlier_lines(log):
    fill(log, 3)
    before = log.path.read_text(encoding="utf-8")
    fill(log, 2)
    after = log.path.read_text(encoding="utf-8")
    assert after.startswith(before), "an append must only ever add to the end of the file"


# --------------------------------------------------------------------------
# Integrity
# --------------------------------------------------------------------------

def test_an_intact_chain_verifies(log):
    fill(log, 7)
    result = log.verify()
    assert result.ok is True
    assert result.events == 7
    assert result.broken_at_seq is None


def test_a_missing_file_verifies_as_empty(tmp_path):
    result = AuditTrail(tmp_path / "nope.jsonl").verify()
    assert result.ok is True
    assert result.events == 0


def test_editing_an_entry_breaks_the_chain(log):
    fill(log, 5)
    records = read_records(log.path)
    records[2]["summary"] = "something else entirely"
    rewrite(log.path, records)

    result = log.verify()
    assert result.ok is False
    assert result.broken_at_seq == 3
    assert "modified" in result.detail


def test_editing_a_payload_breaks_the_chain(log):
    """The payload is where the money is, so it must be covered by the hash."""
    fill(log, 4)
    records = read_records(log.path)
    records[1]["payload"] = {"index": 999999}
    rewrite(log.path, records)
    assert log.verify().broken_at_seq == 2


def test_deleting_an_entry_breaks_the_chain(log):
    fill(log, 5)
    records = read_records(log.path)
    del records[1]
    rewrite(log.path, records)

    result = log.verify()
    assert result.ok is False
    assert "inserted, removed or reordered" in result.detail


def test_reordering_entries_breaks_the_chain(log):
    fill(log, 5)
    records = read_records(log.path)
    records[1], records[2] = records[2], records[1]
    rewrite(log.path, records)
    assert log.verify().ok is False


def test_appending_a_forged_entry_breaks_the_chain(log):
    """A forgery would need the previous hash *and* a matching self-hash."""
    fill(log, 3)
    records = read_records(log.path)
    records.append({
        "seq": 4, "run_id": "r", "at": AT.isoformat(), "stage": "acted",
        "payment_id": "pay_forged", "summary": "we definitely did this", "payload": {},
        "prev_hash": records[-1]["entry_hash"], "entry_hash": "0" * 64,
    })
    rewrite(log.path, records)

    result = log.verify()
    assert result.ok is False
    assert result.broken_at_seq == 4


def test_a_correctly_recomputed_forgery_is_detectable_only_by_the_chain_before_it(log):
    """
    Honest limitation test. Someone who rewrites the whole file from genesis can
    produce a valid chain — hashing detects tampering, it does not prevent it. What
    it guarantees is that a *partial* edit cannot pass, which is the realistic
    threat for a local file.
    """
    fill(log, 3)
    records = read_records(log.path)
    prev = GENESIS_HASH
    records[1]["summary"] = "rewritten"
    for record in records:
        record["prev_hash"] = prev
        record["entry_hash"] = compute_hash(record, prev)
        prev = record["entry_hash"]
    rewrite(log.path, records)
    assert log.verify().ok is True


def test_corrupt_json_is_reported_with_a_line_number(log):
    fill(log, 3)
    with open(log.path, "a", encoding="utf-8") as fh:
        fh.write("{not json at all\n")
    result = log.verify()
    assert result.ok is False
    assert "not valid JSON" in result.detail


def test_a_truncated_tail_does_not_silently_restart_the_chain(log):
    """
    A process killed mid-write leaves a partial final line. Resetting to genesis
    would hide the damage; the next append must chain from something that fails
    verification instead.
    """
    fill(log, 3)
    with open(log.path, "a", encoding="utf-8") as fh:
        fh.write('{"seq": 4, "run_id": "r", "stage": "act')

    reopened = AuditTrail(log.path)
    reopened.append(run_id="r", stage=AuditStage.ACTED, summary="after the crash")

    assert reopened.verify().ok is False


def test_hash_covers_every_recorded_field(log):
    """Each hashed field, changed individually, must break verification."""
    for field, value in [
        ("seq", 99),
        ("run_id", "other_run"),
        ("at", "2000-01-01T00:00:00+00:00"),
        ("stage", "acted"),
        ("payment_id", "pay_other"),
        ("summary", "different"),
        ("payload", {"index": -1}),
    ]:
        trail = AuditTrail(log.path.parent / f"chain_{field}.jsonl")
        fill(trail, 3)
        records = read_records(trail.path)
        records[1][field] = value
        rewrite(trail.path, records)
        assert trail.verify().ok is False, f"changing {field} went undetected"


# --------------------------------------------------------------------------
# Querying
# --------------------------------------------------------------------------

def test_query_filters_by_run(log):
    fill(log, 3, run_id="run_a")
    fill(log, 2, run_id="run_b")
    assert len(log.query(run_id="run_a")) == 3
    assert len(log.query(run_id="run_b")) == 2


def test_query_filters_by_payment_and_stage(log):
    log.append(run_id="r", stage=AuditStage.DECIDED, payment_id="pay_1", at=AT)
    log.append(run_id="r", stage=AuditStage.ACTED, payment_id="pay_1", at=AT)
    log.append(run_id="r", stage=AuditStage.ACTED, payment_id="pay_2", at=AT)

    assert len(log.query(payment_id="pay_1")) == 2
    assert len(log.query(stage=AuditStage.ACTED)) == 2
    assert len(log.query(payment_id="pay_1", stage=AuditStage.ACTED)) == 1


def test_limit_returns_the_most_recent_entries(log):
    fill(log, 10)
    tail = log.query(limit=3)
    assert [e.summary for e in tail] == ["decision 7", "decision 8", "decision 9"]


def test_run_ids_are_listed_oldest_first(log):
    fill(log, 2, run_id="run_a")
    fill(log, 2, run_id="run_b")
    fill(log, 1, run_id="run_a")
    assert log.run_ids() == ["run_a", "run_b"]


# --------------------------------------------------------------------------
# Append-only is structural
# --------------------------------------------------------------------------

def test_the_trail_exposes_no_mutating_operation(log):
    for forbidden in ("update", "delete", "edit", "remove", "truncate", "rewrite"):
        assert not hasattr(log, forbidden), f"AuditTrail should not offer {forbidden}()"


def test_reset_refuses_to_touch_the_configured_trail():
    """
    The application must not be able to decide to start its audit history again.
    conftest redirects the configured path per test, so this points at that.
    """
    configured = AuditTrail(settings.audit_log_path)
    with pytest.raises(RuntimeError, match="Refusing to reset"):
        configured.reset()


def test_reset_works_on_an_explicit_temp_path(log):
    fill(log, 2)
    assert log.path.exists()
    log.reset()
    assert not log.path.exists()
    assert log.append(run_id="r", stage=AuditStage.RUN_STARTED, at=AT).seq == 1


# --------------------------------------------------------------------------
# Hashing helper
# --------------------------------------------------------------------------

def test_hash_is_order_independent_for_dict_keys():
    a = {"seq": 1, "run_id": "r", "at": "t", "stage": "acted", "payment_id": "p",
         "summary": "s", "payload": {"x": 1, "y": 2}}
    b = {"payload": {"y": 2, "x": 1}, "summary": "s", "payment_id": "p", "stage": "acted",
         "at": "t", "run_id": "r", "seq": 1}
    assert compute_hash(a, GENESIS_HASH) == compute_hash(b, GENESIS_HASH)


def test_hash_depends_on_the_previous_hash():
    fields = {"seq": 1, "run_id": "r", "at": "t", "stage": "acted", "payment_id": "p",
              "summary": "s", "payload": {}}
    assert compute_hash(fields, GENESIS_HASH) != compute_hash(fields, "f" * 64)
