"""
Thin wrapper around the Fireworks AI inference API.

Same contract as the old Anthropic wrapper — the rest of the codebase sees:

  is_available()      → bool
  unavailable_reason() → str
  complete(system, user, max_tokens) → (text, error)
  reset_client()
  endpoint()

Two behaviours that matter:

* **Availability is a question, not an exception.** `is_available()` lets the
  diagnose stage decide up front whether to use the LLM path. A missing key is a
  configuration state, not a crash — the agent degrades to rule-based diagnosis
  and says so in the report.
* **Failures are returned, not raised.** `complete()` hands back
  `(text, error)`. One flaky HTTP call must not take down a batch run, and the
  error string has to survive into the audit trail rather than a stack trace.
"""

from __future__ import annotations

import json

import requests

from app.config import settings

_FIREWORKS_URL = "https://api.fireworks.ai/inference/v1/chat/completions"

# Session is re-used across calls so TCP connections are pooled.
_session: requests.Session | None = None


def is_available() -> bool:
    """Whether a real LLM call could be attempted at all."""
    return bool(settings.fireworks_api_key)


def unavailable_reason() -> str:
    """Why not, in words fit for the audit trail."""
    if not settings.fireworks_api_key:
        return "FIREWORKS_API_KEY is not set"
    return ""


def endpoint() -> str:
    """Which host diagnosis will talk to. Surfaced on /health."""
    return _FIREWORKS_URL


def get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update(
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {settings.fireworks_api_key}",
            }
        )
    return _session


def reset_client() -> None:
    """Drop the cached session. Used by tests that swap credentials."""
    global _session
    _session = None


def complete(system: str, user: str, max_tokens: int | None = None) -> tuple[str, str]:
    """
    Send one prompt, return `(text, error)`.

    Exactly one of the two is non-empty. Temperature is set to 0 so a diagnosis
    is deterministic across identical runs — a metric that moves when nothing
    moved is not a metric.
    """
    if not is_available():
        return "", f"LLM unavailable: {unavailable_reason()}"

    payload = {
        "model": settings.fireworks_model,
        "max_tokens": max_tokens or settings.llm_max_tokens,
        "top_k": 40,
        "temperature": 0,
        "presence_penalty": 0,
        "frequency_penalty": 0,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }

    try:
        resp = get_session().post(
            _FIREWORKS_URL,
            data=json.dumps(payload),
            timeout=settings.llm_timeout_seconds,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.Timeout:
        return "", "Fireworks request timed out"
    except requests.exceptions.HTTPError as exc:
        return "", f"HTTPError {exc.response.status_code}: {exc.response.text[:200]}"
    except Exception as exc:
        return "", f"{type(exc).__name__}: {exc}"

    try:
        text = data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, AttributeError) as exc:
        return "", f"Unexpected response shape: {exc} — raw: {str(data)[:200]}"

    if not text:
        return "", "model returned no text content"
    return text, ""
