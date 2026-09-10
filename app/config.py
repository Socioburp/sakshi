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
    # Insights (reach, saves, shares per post) need instagram_business_manage_insights,
    # which App Review grants. Flip this once it is approved; the connect link
    # then asks for it and the sync starts reading numbers.
    ig_insights_enabled: bool = False

    # stt
    stt_provider: Literal["mock", "elevenlabs", "deepgram", "sarvam"] = "mock"
    elevenlabs_api_key: str = ""
    deepgram_api_key: str = ""
    sarvam_api_key: str = ""

    # imagegen -- three vendors for the same open-weight FLUX family, one
    # interface. Model ids are per vendor; the price is what the ledger records.
    imagegen_provider: Literal["mock", "fal", "replicate", "bfl"] = "mock"
    fal_key: str = ""
    imagegen_fal_model: str = "fal-ai/flux/schnell"  # or fal-ai/flux-2/klein/4b
    replicate_api_token: str = ""
    imagegen_replicate_model: str = "black-forest-labs/flux-schnell"
    bfl_api_key: str = ""
    imagegen_bfl_model: str = "flux-2-klein-4b"  # or flux-2-pro
    # Vendor price per image in micro-dollars, for the ledger only. Unset (0)
    # means the per-vendor list price in providers.DEFAULT_COST_MICROS.
    imagegen_cost_micros: int = 0

    # compositor. Empty = the Chromium `playwright install` fetched; set it to
    # use a Chromium the host already ships (a path to the `chrome` binary).
    chromium_executable: str = ""

    # product lane: the owner's photo, product kept, background replaced
    cutout_enabled: bool = True
    # rembg model. isnet-general-use is MIT-licensed, ~1.2GB RSS at 1024px and
    # ~2s on a small CPU. birefnet-general-lite is sharper but needs >4GB.
    # bria-rmbg (rembg's default) is NOT licensed for commercial use.
    cutout_model: str = "isnet-general-use"

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
