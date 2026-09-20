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
    # Webhook verification (comments and DMs). Falls back to WA_VERIFY_TOKEN.
    ig_verify_token: str = ""
    # Replying to comments and DMs needs the manage_comments / manage_messages
    # permissions from App Review. Flip on once granted; the connect link then
    # asks for them and the reply loop goes live.
    ig_engagement_enabled: bool = False

    # stt
    stt_provider: Literal["mock", "elevenlabs", "deepgram", "sarvam"] = "mock"
    elevenlabs_api_key: str = ""
    deepgram_api_key: str = ""
    sarvam_api_key: str = ""

    # imagegen -- the FLUX family (fal/replicate/bfl) plus OpenAI's gpt-image-1,
    # one interface. Model ids are per vendor; the price is what the ledger records.
    #
    # DEFAULTS ARE THE QUALITY TIER, NOT THE CHEAP TIER.
    # Every default here used to be the fast/distilled variant -- schnell at 4
    # steps, klein 4B, gpt-image-1 at "medium" -- which is why creatives came
    # back soft, plasticky and obviously generated. Those models are built to
    # win on latency, not on whether an owner would put the picture on their
    # grid. The product promise is "a 25-year marketing creator made this", so
    # the default is `dev` at full steps and the cheap tier is opt-in.
    imagegen_provider: Literal["mock", "fal", "replicate", "bfl", "openai"] = "mock"
    fal_key: str = ""
    imagegen_fal_model: str = "fal-ai/flux/dev"  # schnell is the cheap tier
    replicate_api_token: str = ""
    imagegen_replicate_model: str = "black-forest-labs/flux-dev"
    bfl_api_key: str = ""
    imagegen_bfl_model: str = "flux-2-pro"  # klein-4b is the cheap tier
    openai_api_key: str = ""
    # The dated snapshot, not the floating alias: the alias moves under you and
    # the look of every brand's grid moves with it. Change it here or with
    # IMAGEGEN_OPENAI_MODEL, deliberately, never by surprise.
    imagegen_openai_model: str = "gpt-image-2-2026-04-21"
    # There is no quality setting. It is "high", always, for a single post and
    # for every slide of a carousel (providers.OPENAI_QUALITY). Quality is never
    # traded for speed or cost; a slow job says so in the chat instead.
    #
    # The size the picture is GENERATED at: native 4:5, both edges multiples of
    # 16, above the 1080x1350 it is delivered at and below the pixel count
    # (2560x1440) past which OpenAI marks resolutions experimental. 1728x2160 is
    # the next step up and sits just past that line -- verify before using it.
    # 1080x1350 is not a legal generation size: 1080 is not a multiple of 16.
    imagegen_size: str = "1600x2000"
    # Slides of a carousel are generated in parallel, this many at a time.
    # OpenAI tier 1 is 5 images/minute for gpt-image-2; a 429 is retried with
    # the vendor's retry-after, never answered with a cheaper call.
    imagegen_concurrency: int = 4
    # The background gate: every generated picture is inspected and a rejected
    # one is regenerated with a corrected prompt, up to this many attempts per
    # slide. On exhaustion the slide FAILS and is refunded; a rejected picture
    # is never delivered. Each attempt is a paid vendor call.
    imagegen_gate_attempts: int = 6
    # ...and never more than this much vendor spend on one slide, in micro-dollars.
    # The owner pays one credit however many attempts it takes, so an unbounded
    # gate is an unbounded loss. At ~$0.29 a call this allows three honest tries;
    # a prompt that fails three times is a prompt problem, not bad luck. The cap
    # stops the RETRYING -- it never lowers the settings of a call. 0 = no cap.
    imagegen_gate_budget_micros: int = 900_000
    # Seconds of silence after which the owner is told the job is still going.
    # A metric to watch and a message to send -- never a limit on the output.
    slow_notice_s: int = 60
    # How a carousel reaches the owner. "ordered": slides are held until the set
    # is done and sent 1..N, so the chat reads in order and the set can be
    # forwarded as it stands; progress is reported in words meanwhile.
    # "as_ready": each slide is sent the moment it finishes, captioned with its
    # place, and may arrive out of order. A single post is always sent at once.
    carousel_delivery: Literal["ordered", "as_ready"] = "ordered"
    # Denoising steps for the step-taking FLUX models. schnell is distilled to
    # 4 and ignores more; dev is trained for ~28 and visibly improves up to it.
    # 0 means "the right number for the model", resolved in providers.py.
    imagegen_steps: int = 0
    # PNG out of the vendor, JPEG once at the end. Asking a vendor for JPEG
    # meant the background was lossily encoded, composited over, screenshotted
    # and encoded again -- two generation losses before the owner saw it.
    imagegen_lossless_source: bool = True
    # Vendor price per image in micro-dollars, for the ledger only. Unset (0)
    # means the per-vendor list price in providers.DEFAULT_COST_MICROS.
    imagegen_cost_micros: int = 0

    # compositor. Empty = the Chromium `playwright install` fetched; set it to
    # use a Chromium the host already ships (a path to the `chrome` binary).
    chromium_executable: str = ""
    # Refuse to render when a face the creative is set in did not load, rather
    # than ship the brand in a fallback face. Off only where there is no
    # network to fetch fonts from (the test suite).
    compose_require_fonts: bool = True

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
    # A credit is now ~$0.29-0.90 of vendor spend (gpt-image-2 high, plus gate
    # retries), not the ~$0.03 it was when this was 10. Ten free credits was up
    # to ~$9 handed to every signup; three is a fair look at the product.
    free_trial_credits: int = 3

    @property
    def is_prod(self) -> bool:
        return self.env == "prod"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
