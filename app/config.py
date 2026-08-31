from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """
    Central app config. Values load from .env automatically.
    Never hardcode secrets anywhere else in the codebase — read them from here.
    """

    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    fireworks_api_key: str = ""

    #: Contact policy. These two are the whole of "don't harass the customer":
    #: never more than max_retry_attempts touches, never two touches inside the
    #: cooldown window. Enforced in app/decide.py by deterministic code.
    max_retry_attempts: int = 3
    retry_cooldown_hours: int = 6

    #: Diagnose stage. The LLM is advisory only — see app/diagnose.py. If the key
    #: is absent or the call fails, diagnosis degrades to the rule-based path
    #: rather than the run failing.
    fireworks_model: str = "accounts/fireworks/models/glm-5p3"
    llm_max_tokens: int = 512
    llm_timeout_seconds: float = 20.0

    #: Seed for the settlement outcome model. Fixed so the "modeled" column of
    #: the metrics report is reproducible run to run for a given batch.
    outcome_model_seed: int = 42

    #: Append-only audit trail. One JSON object per line, hash-chained.
    audit_log_path: Path = PROJECT_ROOT / "data" / "audit_log.jsonl"
    #: fsync every audit entry. On by default: an entry sitting in an OS buffer
    #: when the machine loses power is not an audit entry. Costs roughly a second
    #: per batch run, which is the right trade for a durability guarantee — the
    #: test suite turns it off because it has no durability to protect.
    audit_fsync: bool = True

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        # Ignore unrelated variables in .env rather than failing to boot. The
        # default is to reject them, which turns an unrelated stray line in a
        # local .env into a hard startup crash.
        extra="ignore",
    )


settings = Settings()
