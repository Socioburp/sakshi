"""Single source of truth for configuration. Nothing else reads os.environ."""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # core
    env: Literal["dev", "staging", "prod"] = "dev"
    log_level: str = "INFO"
    public_base_url: str = "http://localhost:8000"

    # postgres (Neon, pooled)
    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/sakshi"
    db_pool_size: int = 5
    db_max_overflow: int = 5

    # redis (Upstash)
    redis_url: str = "redis://localhost:6379/0"
    queue_name: str = "sakshi:jobs"

    # anthropic
    anthropic_api_key: str = ""
    anthropic_model: str = ""  # copy the exact id from console.anthropic.com
    agent_max_turns: int = 12
    agent_max_tokens: int = 4096

    # voyage
    voyage_api_key: str = ""
    voyage_model: str = "voyage-3"
    embed_dim: int = 1024

    # r2
    r2_account_id: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_bucket: str = "sakshi-creatives"
    r2_public_base_url: str = ""

    # whatsapp
    wa_provider: Literal["meta", "gupshup", "twilio", "mock"] = "mock"
    wa_verify_token: str = "dev-verify-token"
    wa_app_secret: str = ""
    wa_access_token: str = ""
    wa_phone_number_id: str = ""
    wa_graph_version: str = "v21.0"
    gupshup_api_key: str = ""
    gupshup_source: str = ""
    gupshup_app_name: str = ""
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from: str = ""

    # instagram (Track A)
    instagram_mock: bool = True
    ig_app_id: str = ""
    ig_app_secret: str = ""
    ig_redirect_uri: str = ""

    # stt
    stt_provider: Literal["mock", "elevenlabs", "deepgram", "sarvam"] = "mock"
    elevenlabs_api_key: str = ""
    deepgram_api_key: str = ""
    sarvam_api_key: str = ""

    # imagegen
    imagegen_provider: Literal["mock", "provider_a", "provider_b"] = "mock"
    imagegen_a_api_key: str = ""
    imagegen_b_api_key: str = ""

    # billing
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""
    free_trial_credits: int = 10

    @property
    def is_prod(self) -> bool:
        return self.env == "prod"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
