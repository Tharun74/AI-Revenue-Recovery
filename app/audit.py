"""
Append-only audit trail.

One JSON object per line in `data/audit_log.jsonl`. Every decision the agent
makes and every action it takes lands here, in order, with the reasoning
attached.

**Append-only is structural, not a promise.** This module offers `append`,
`read_all` and `verify`. There is no update, no delete and no rewrite, and the
file is only ever opened in `"a"` mode. The single exception is `reset()`, which
exists solely for tests and takes an explicit path — it will refuse to run
against the configured production trail.

**Tamper-evident.** Each entry stores the hash of the previous entry, and its own
hash covers both its content and that `prev_hash`. Editing an old entry, deleting
one, or reordering two of them all break the chain from that point on, and
`verify()` reports exactly which sequence number broke. An audit trail that can be
quietly rewritten is decoration; this one can be checked.

Why hand-rolled JSONL rather than a database: the trail has to be readable by a
reviewer with `less` and diffable in a pull request. That is worth more here than
query flexibility.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from app.config import settings
from app.models import AuditEvent, AuditStage, AuditVerification, utcnow

#: Fields covered by the hash. Listed explicitly so that adding a field to
#: AuditEvent later cannot silently change how historical entries hash.
_HASHED_FIELDS = ("seq", "run_id", "at", "stage", "payment_id", "summary", "payload")

GENESIS_HASH = "0" * 64


def _canonical(obj: Any) -> str:
    """Stable JSON for hashing: sorted keys, no incidental whitespace."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def compute_hash(event_fields: dict, prev_hash: str) -> str:
    body = _canonical({k: event_fields.get(k) for k in _HASHED_FIELDS})
    return hashlib.sha256(f"{prev_hash}{body}".encode("utf-8")).hexdigest()


def _jsonable(payload: Any) -> Any:
    """Coerce pydantic models / datetimes / enums into plain JSON structures."""
    if hasattr(payload, "model_dump"):
        return payload.model_dump(mode="json")
    if isinstance(payload, dict):
        return {str(k): _jsonable(v) for k, v in payload.items()}
    if isinstance(payload, (list, tuple)):
        return [_jsonable(v) for v in payload]
    if isinstance(payload, datetime):
        return payload.isoformat()
    if isinstance(payload, (str, int, float, bool)) or payload is None:
        return payload
    return str(payload)


def _read_last_line(path: Path) -> str:
    """
    Read only the final line of the file.

    Seeks from the end rather than reading the whole log, because `append` needs
    the previous hash on every single write — reading the file each time would
    make writing an 88-case run quadratic in the log's size.
    """
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        end = fh.tell()
        if end == 0:
            return ""
        block = 4096
        buffer = b""
        pos = end
        while pos > 0:
            step = min(block, pos)
            pos -= step
            fh.seek(pos)
            buffer = fh.read(step) + buffer
            stripped = buffer.rstrip(b"\r\n")
            if b"\n" in stripped:
                return stripped.rsplit(b"\n", 1)[1].decode("utf-8")
            if pos == 0:
                return stripped.decode("utf-8")
        return ""


class AuditTrail:
    """
    A handle on one append-only log file.

    The lock matters: FastAPI serves requests on a thread pool, so two runs could
    otherwise interleave writes and produce a chain where `prev_hash` no longer
    matches the physically preceding line.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else Path(settings.audit_log_path)
        self._lock = threading.Lock()
        self._last_seq: Optional[int] = None
        self._last_hash: Optional[str] = None

    # -- writing ----------------------------------------------------------

    def _load_tip(self) -> tuple[int, str]:
        """The (seq, hash) of the last entry, or (0, GENESIS_HASH) if empty."""
        if self._last_seq is not None and self._last_hash is not None:
            return self._last_seq, self._last_hash
        if not self.path.exists():
            self._last_seq, self._last_hash = 0, GENESIS_HASH
            return self._last_seq, self._last_hash
        line = _read_last_line(self.path)
        if not line.strip():
            self._last_seq, self._last_hash = 0, GENESIS_HASH
            return self._last_seq, self._last_hash
        try:
            record = json.loads(line)
            self._last_seq = int(record["seq"])
            self._last_hash = str(record["entry_hash"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            # A truncated final line (killed mid-write) must not silently reset
            # the chain to genesis — that would hide the damage. Chain from a
            # marker instead, so verify() reports the break at this point.
            self._last_seq = -1
            self._last_hash = "unreadable-tail"
        return self._last_seq, self._last_hash

    def append(
        self,
        run_id: str,
        stage: AuditStage,
        summary: str = "",
        payment_id: str = "",
        payload: Any = None,
        at: Optional[datetime] = None,
    ) -> AuditEvent:
        """Write one entry and return it. The only mutating operation here."""
        with self._lock:
            prev_seq, prev_hash = self._load_tip()
            fields = {
                "seq": prev_seq + 1,
                "run_id": run_id,
                "at": (at or utcnow()).isoformat(),
                "stage": stage.value if isinstance(stage, AuditStage) else str(stage),
                "payment_id": payment_id,
                "summary": summary,
                "payload": _jsonable(payload) if payload is not None else {},
            }
            entry_hash = compute_hash(fields, prev_hash)
            record = {**fields, "prev_hash": prev_hash, "entry_hash": entry_hash}

            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(_canonical(record) + "\n")
                fh.flush()
                if settings.audit_fsync:
                    os.fsync(fh.fileno())

            self._last_seq = fields["seq"]
            self._last_hash = entry_hash
            return AuditEvent(**record)

    def append_many(self, events: Iterable[dict]) -> list[AuditEvent]:
        """Convenience for a burst of entries. Still one append per entry."""
        return [self.append(**e) for e in events]

    # -- reading ----------------------------------------------------------

    def read_all(self) -> list[AuditEvent]:
        if not self.path.exists():
            return []
        events: list[AuditEvent] = []
        with open(self.path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(AuditEvent(**json.loads(line)))
                except Exception:
                    # Skip for reading; verify() is what reports the damage.
                    continue
        return events

    def query(
        self,
        run_id: Optional[str] = None,
        payment_id: Optional[str] = None,
        stage: Optional[AuditStage] = None,
        limit: Optional[int] = None,
    ) -> list[AuditEvent]:
        events = self.read_all()
        if run_id:
            events = [e for e in events if e.run_id == run_id]
        if payment_id:
            events = [e for e in events if e.payment_id == payment_id]
        if stage:
            events = [e for e in events if e.stage is stage]
        if limit is not None:
            events = events[-limit:]
        return events

    def run_ids(self) -> list[str]:
        """Distinct run ids, oldest first."""
        seen: dict[str, None] = {}
        for event in self.read_all():
            seen.setdefault(event.run_id, None)
        return list(seen)

    # -- integrity --------------------------------------------------------

    def verify(self) -> AuditVerification:
        """
        Re-walk the chain from genesis and recompute every hash.

        Checks three things: each line parses, each `seq` increments by exactly
        one, and each `entry_hash` matches a recomputation over the entry plus its
        recorded `prev_hash`. The first failure is reported with its seq — later
        entries are all invalid by construction once the chain breaks, so listing
        them adds noise, not information.
        """
        if not self.path.exists():
            return AuditVerification(path=str(self.path), events=0, ok=True, detail="No audit log yet.")

        prev_hash = GENESIS_HASH
        expected_seq = 1
        count = 0

        with open(self.path, encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    return AuditVerification(
                        path=str(self.path), events=count, ok=False, broken_at_seq=expected_seq,
                        detail=f"Line {line_no} is not valid JSON: {exc}",
                    )

                seq = record.get("seq")
                if seq != expected_seq:
                    return AuditVerification(
                        path=str(self.path), events=count, ok=False, broken_at_seq=expected_seq,
                        detail=f"Line {line_no}: expected seq {expected_seq}, found {seq!r} — an "
                               "entry was inserted, removed or reordered.",
                    )
                if record.get("prev_hash") != prev_hash:
                    return AuditVerification(
                        path=str(self.path), events=count, ok=False, broken_at_seq=seq,
                        detail=f"Entry {seq}: prev_hash does not match the preceding entry's hash.",
                    )
                recomputed = compute_hash(record, prev_hash)
                if recomputed != record.get("entry_hash"):
                    return AuditVerification(
                        path=str(self.path), events=count, ok=False, broken_at_seq=seq,
                        detail=f"Entry {seq}: content has been modified since it was written "
                               "(recomputed hash does not match the stored one).",
                    )

                prev_hash = record["entry_hash"]
                expected_seq += 1
                count += 1

        return AuditVerification(
            path=str(self.path), events=count, ok=True,
            detail=f"Chain intact across {count} entries.",
        )

    # -- test support -----------------------------------------------------

    def reset(self) -> None:
        """
        Delete the log. Exists for tests only, and refuses to touch the
        configured trail — the whole value of an append-only file is that the
        application cannot decide to start again.
        """
        if self.path.resolve() == Path(settings.audit_log_path).resolve():
            raise RuntimeError(
                "Refusing to reset the configured audit trail. Point AuditTrail at a "
                "temporary path if you need a clean log."
            )
        if self.path.exists():
            self.path.unlink()
        self._last_seq = None
        self._last_hash = None


#: Process-wide default trail, used by the API and the orchestrator.
default_trail = AuditTrail()


__all__ = ["GENESIS_HASH", "AuditTrail", "compute_hash", "default_trail"]
