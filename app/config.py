from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Central app config. Values load from .env automatically.
    Never hardcode secrets anywhere else in the codebase — read them from here.
    """

    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    anthropic_api_key: str = ""

    max_retry_attempts: int = 3
    retry_cooldown_hours: int = 6

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        # Ignore unrelated variables in .env rather than failing to boot. The
        # default is to reject them, which turns an unrelated stray line in a
        # local .env into a hard startup crash.
        extra="ignore",
    )


settings = Settings()
