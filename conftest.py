"""
Shared pytest setup.

Two autouse fixtures enforce properties the suite would otherwise only hope for:

* **The audit trail is redirected to a temp file.** `data/audit_log.jsonl` is
  append-only by design, so a test run that wrote to it would permanently
  contaminate the real record with fixture data.
* **No test may make a live API call.** `get_client` on both service wrappers is
  replaced with something that raises. A test that needs the network must say so
  with `@pytest.mark.live`, which makes the network-touching tests a short,
  greppable list rather than an assumption.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.audit import default_trail  # noqa: E402
from app.config import settings  # noqa: E402
from app.services import llm_client, razorpay_client  # noqa: E402


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "live: test makes real outbound API calls (deselect with -m 'not live')"
    )


@pytest.fixture(autouse=True)
def isolated_audit_trail(tmp_path, monkeypatch):
    """Point every trail at a per-test temp file, and skip fsync — there is no
    durability to protect in a temp directory, and per-entry fsync costs the suite
    about a second per batch run."""
    log = tmp_path / "audit_log.jsonl"
    monkeypatch.setattr(settings, "audit_log_path", log)
    monkeypatch.setattr(settings, "audit_fsync", False)
    monkeypatch.setattr(default_trail, "path", log)
    monkeypatch.setattr(default_trail, "_last_seq", None, raising=False)
    monkeypatch.setattr(default_trail, "_last_hash", None, raising=False)
    yield


class LiveCallAttempted(BaseException):
    """
    Raised when a test tries to reach a real API.

    Derives from BaseException, not Exception, on purpose: both service wrappers
    deliberately catch `Exception` so a flaky provider cannot abort a batch. A
    guard that inherited from Exception would be swallowed by exactly that
    handling and the escape would pass silently.
    """


@pytest.fixture(autouse=True)
def block_live_calls(request, monkeypatch):
    """
    Make outbound calls impossible unless the test is marked `live`.

    Deliberately patches `get_client` rather than the individual call functions:
    that way a newly added capability in either service wrapper is blocked by
    default instead of silently escaping the sandbox.
    """
    if request.node.get_closest_marker("live"):
        yield
        return

    def refuse(*_args, **_kwargs):
        raise LiveCallAttempted(
            "This test attempted a live API call. Mark it @pytest.mark.live if that is "
            "intended, or stub the service wrapper."
        )

    monkeypatch.setattr(razorpay_client, "get_client", refuse)
    monkeypatch.setattr(llm_client, "get_session", refuse)
    yield


@pytest.fixture
def trail(tmp_path):
    """A standalone audit trail, for tests that want to inspect one directly."""
    from app.audit import AuditTrail

    return AuditTrail(tmp_path / "explicit_trail.jsonl")
