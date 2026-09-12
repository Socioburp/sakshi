"""Image-generation adapters.

Only backgrounds come out of here -- never text. Three vendors serve the same
open-weight model family (FLUX, Black Forest Labs) behind one interface, so
they can be A/B'd on the same briefs and switched with an env var:

    fal        POST https://fal.run/{model}            Authorization: Key
    replicate  POST /v1/models/{model}/predictions    Authorization: Bearer
    bfl        POST https://api.bfl.ai/v1/{model}     x-key   (submit + poll)

Each vendor's request/response shape is taken from its published API
reference (see the class docstrings). The shared plumbing in
`HttpImageProvider` is where the money is protected:

* Only the SUBMIT is retried on 429/5xx. Polling and the download have their
  own bounded retries, so a transient error while waiting never re-submits
  a generation the vendor is already running and billing.
* One hard budget per call (`BUDGET_S`). The worker is a single consumer;
  a vendor that stalls must cost seconds, not every owner's next reply.
* The picture is downloaded and gated: blank, tiny or undecodable output
  fails loudly, before a credit is spent compositing over it.

FLUX models take no negative prompt (BFL: "Most FLUX models do not support
negative prompts"), so `flux_prompt()` folds the brief's negative into the
positive phrasing the model does respond to. The brief contract -- which
still carries a negative for vendors that use one -- is unchanged.
"""

from __future__ import annotations

import asyncio
import base64
import io
import re
import time
from typing import Any

import httpx

from app.config import settings
from app.creative.imagegen.base import ImageRequest, ImageResult
from app.creative.photoreal import CAMERA_MARKER
from app.logging import get_logger

log = get_logger(__name__)


class ImageGenError(RuntimeError):
    """The vendor answered, but not with a usable picture."""


class BlankImageError(ImageGenError):
    """Decodable, but (near-)uniform: the classic silent failure."""


class MockImageProvider:
    name = "mock"
    cost_micros_per_image = 0

    async def generate(self, req: ImageRequest) -> ImageResult:
        # A deterministic gradient, so compositing and layout can be developed
        # and tested without spending money or waiting on a vendor.
        from PIL import Image, ImageDraw  # local import: only the mock needs Pillow

        img = Image.new("RGB", (req.width, req.height), "#E8C9A0")
        d = ImageDraw.Draw(img)
        for y in range(req.height):
            t = y / req.height
            d.line(
                [(0, y), (req.width, y)],
                fill=(int(232 - 90 * t), int(201 - 70 * t), int(160 - 60 * t)),
            )
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=88)
        return ImageResult(data=buf.getvalue(), mime="image/jpeg", provider=self.name, latency_ms=1)


# --------------------------------------------------------------------------- #
# prompt shaping for the FLUX family
# --------------------------------------------------------------------------- #
# Positive replacements for the things the pipeline puts in the negative.
# Source: Black Forest Labs' own replacement table -- "describe what you DO
# want". Adjectival on purpose: a replacement must not add a noun the brief
# never asked for. Keys are matched as whole words against the negative.
_NEGATIVE_TO_POSITIVE: list[tuple[tuple[str, ...], str]] = [
    (
        ("text", "letters", "words", "caption", "typography", "signature", "watermark", "logo"),
        "clean unmarked surfaces",
    ),
    (
        ("3d render", "cgi", "digital art", "illustration", "painting", "concept art"),
        "a real photograph with authentic detail",
    ),
    (
        ("airbrushed", "plastic surfaces", "waxy", "glossy cgi highlights", "overprocessed"),
        "natural material texture with small imperfections kept",
    ),
    (
        ("oversaturated", "hdr", "unrealistic lighting", "neon glow", "surreal"),
        "true-to-life colour and believable light",
    ),
    (
        ("low quality", "jpeg artifacts", "blurry", "deformed", "distorted hands", "extra fingers"),
        "tack-sharp focus and natural proportions",
    ),
    (
        ("perfectly symmetrical", "duplicated objects", "mangled anatomy", "extra limbs"),
        "natural asymmetry",
    ),
]

# FLUX.1 [schnell] reads 256 T5 tokens (the default on fal and Replicate);
# ~1000 characters of English. Anything past that is silently dropped, so
# the prompt is trimmed here, at a clause boundary, where it can be seen.
FLUX_PROMPT_MAX = 1000
# The camera clause photoreal.photographic() appends; the folded positives go
# in front of it so that, when something has to go, it is the tail.
_CAMERA_MARKER = CAMERA_MARKER


def _trim_at_clause(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for sep in (". ", "; ", ", "):
        i = cut.rfind(sep)
        if i > limit // 2:
            return cut[: i + 1].rstrip(",; ").rstrip(".") + "."
    return cut.rsplit(" ", 1)[0].rstrip(",;. ") + "."


def flux_prompt(prompt: str, negative: str | None) -> str:
    """One positive prose prompt for a model that takes no negative.

    The brief's negative list is translated clause by clause into what should
    be there instead; clauses with no mapping are dropped rather than sent as
    "no X", which BFL documents as making the model focus MORE on X.
    """
    p = " ".join((prompt or "").split()).rstrip(".,; ")
    neg = (negative or "").lower()
    keys = {s.strip() for s in neg.split(",") if s.strip()}
    adds: list[str] = []
    for words, positive in _NEGATIVE_TO_POSITIVE:
        if any(w in keys or re.search(rf"(?<![a-z]){re.escape(w)}(?![a-z])", neg) for w in words):
            adds.append(positive)
    if not adds:
        return _trim_at_clause(f"{p}.", FLUX_PROMPT_MAX)
    folded = "; ".join(dict.fromkeys(adds))
    folded = folded[0].upper() + folded[1:]
    i = p.find(_CAMERA_MARKER)
    if i > 0:
        head, tail = p[:i].rstrip(".,; "), p[i:]
        out = f"{head}. {folded}. {tail}."
    else:
        out = f"{p}. {folded}."
    return _trim_at_clause(out, FLUX_PROMPT_MAX)


# --------------------------------------------------------------------------- #
# shared plumbing
# --------------------------------------------------------------------------- #
RETRY_STATUSES = {429, 500, 502, 503, 504}
MIN_EDGE = 256
MAX_DOWNLOAD_BYTES = 30 * 1024 * 1024

# Vendor list price per image, in micro-dollars, for the ledger. Overridden
# for every vendor at once by IMAGEGEN_COST_MICROS when set.
DEFAULT_COST_MICROS = {
    "fal": 4400,  # FLUX.1 [schnell] $0.003/MP, a 1080x1350 post is 1.46MP
    "replicate": 3000,  # flux-schnell "$3.00 / thousand output images"
    "bfl": 14000,  # FLUX.2 [klein] 4B, 1.4c for the first megapixel
    "openai": 40000,  # gpt-image-1 medium, ~4c for a 1024-edge image
}


def _cost_for(name: str) -> int:
    override = settings.imagegen_cost_micros
    return int(override) if override else DEFAULT_COST_MICROS.get(name, 0)


class HttpImageProvider:
    """Shared plumbing. Subclasses implement `_submit` and `_wait`."""

    name = "http"
    cost_micros_per_image = 0
    # connect / read / write / pool. Read covers a synchronous vendor holding
    # the request open while it generates.
    TIMEOUT = httpx.Timeout(connect=10.0, read=90.0, write=30.0, pool=10.0)
    BUDGET_S = 170.0  # everything: submit, wait, download, gate
    SUBMIT_ATTEMPTS = 3
    BACKOFF = (1.0, 4.0)  # seconds before submit attempt 2 and 3
    GET_ATTEMPTS = 3  # for each poll / download GET

    def _check_key(self) -> None:
        raise NotImplementedError

    async def _submit(self, client: httpx.AsyncClient, req: ImageRequest) -> dict[str, Any]:
        """One POST. Returns a job dict the vendor can be asked about."""
        raise NotImplementedError

    async def _wait(
        self, client: httpx.AsyncClient, job: dict[str, Any], req: ImageRequest
    ) -> dict[str, Any]:
        """Poll until the picture exists.

        Returns {"url" | "data", "mime", "job_id", "seed", "raw"}.
        """
        raise NotImplementedError

    async def generate(self, req: ImageRequest) -> ImageResult:
        self._check_key()
        t0 = time.perf_counter()
        try:
            out = await asyncio.wait_for(self._generate(req), self.BUDGET_S)
        except TimeoutError as exc:
            raise ImageGenError(f"{self.name}: over the {self.BUDGET_S:.0f}s budget") from exc
        ms = int((time.perf_counter() - t0) * 1000)
        log.info("imagegen_ok", provider=self.name, ms=ms, bytes=len(out["data"]))
        return ImageResult(
            data=out["data"],
            mime=out["mime"],
            provider=self.name,
            job_id=out.get("job_id"),
            cost_micros=self.cost_micros_per_image,
            latency_ms=ms,
            seed=out.get("seed", req.seed),
            raw=out.get("raw") or {},
        )

    async def _generate(self, req: ImageRequest) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.TIMEOUT) as client:
            job = await self._submit_with_retry(client, req)
            out = await self._wait(client, job, req)
            data, mime = out.get("data"), out.get("mime")
            if data is None:
                data, mime = await self._download(client, out["url"], mime)
            data, mime = await asyncio.to_thread(self._gate, data, mime or "image/jpeg")
            return {**out, "data": data, "mime": mime}

    async def _submit_with_retry(
        self, client: httpx.AsyncClient, req: ImageRequest
    ) -> dict[str, Any]:
        """The only thing retried on 429/5xx: nothing has been generated yet."""
        for attempt in range(self.SUBMIT_ATTEMPTS):
            try:
                return await self._submit(client, req)
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status not in RETRY_STATUSES or attempt == self.SUBMIT_ATTEMPTS - 1:
                    raise ImageGenError(
                        f"{self.name}: HTTP {status}: {exc.response.text[:300]}"
                    ) from exc
                wait = self.BACKOFF[min(attempt, len(self.BACKOFF) - 1)]
                retry_after = exc.response.headers.get("retry-after")
                if retry_after and retry_after.isdigit():
                    wait = max(wait, min(float(retry_after), 30.0))
                log.warning("imagegen_retry", provider=self.name, status=status, wait=wait)
                await asyncio.sleep(wait)
            except (TimeoutError, httpx.TransportError) as exc:
                if attempt == self.SUBMIT_ATTEMPTS - 1:
                    raise ImageGenError(f"{self.name}: submit failed: {exc!r}") from exc
                await asyncio.sleep(self.BACKOFF[min(attempt, len(self.BACKOFF) - 1)])
        raise ImageGenError(f"{self.name}: submit gave up")  # unreachable

    async def _get(
        self, client: httpx.AsyncClient, url: str, headers: dict[str, str] | None = None
    ) -> httpx.Response:
        """A GET that survives a transient blip without touching the submit."""
        last: Exception | None = None
        for attempt in range(self.GET_ATTEMPTS):
            try:
                r = await client.get(url, headers=headers, follow_redirects=True)
                if r.status_code in RETRY_STATUSES and attempt < self.GET_ATTEMPTS - 1:
                    await asyncio.sleep(1.0 + attempt)
                    continue
                r.raise_for_status()
                return r
            except (TimeoutError, httpx.TransportError) as exc:
                last = exc
                if attempt < self.GET_ATTEMPTS - 1:
                    await asyncio.sleep(1.0 + attempt)
            except httpx.HTTPStatusError as exc:
                raise ImageGenError(
                    f"{self.name}: GET {exc.response.status_code}: {exc.response.text[:200]}"
                ) from exc
        raise ImageGenError(f"{self.name}: GET failed: {last!r}")

    @staticmethod
    def _json(r: httpx.Response, what: str) -> dict[str, Any]:
        try:
            body = r.json()
        except ValueError as exc:
            raise ImageGenError(f"{what}: not JSON: {r.text[:200]}") from exc
        if not isinstance(body, dict):
            raise ImageGenError(f"{what}: unexpected body: {str(body)[:200]}")
        return body

    async def _download(
        self, client: httpx.AsyncClient, url: str, mime_hint: str | None
    ) -> tuple[bytes, str]:
        if url.startswith("data:"):
            head, _, body = url.partition(",")
            mime = head[5:].split(";")[0] or (mime_hint or "image/jpeg")
            return base64.b64decode(body), mime
        if not url.startswith("https://"):
            raise ImageGenError(f"{self.name}: refusing non-https result url")
        r = await self._get(client, url)
        if len(r.content) > MAX_DOWNLOAD_BYTES:
            raise ImageGenError(f"{self.name}: result too large ({len(r.content)} bytes)")
        mime = (r.headers.get("content-type") or mime_hint or "image/jpeg").split(";")[0]
        return r.content, mime

    @staticmethod
    def _gate(data: bytes, mime: str) -> tuple[bytes, str]:
        """Refuse what the compositor cannot use: unreadable, tiny, or blank."""
        from PIL import Image, ImageStat

        if not data:
            raise BlankImageError("empty body")
        try:
            im = Image.open(io.BytesIO(data))
            im.load()
        except Exception as exc:  # noqa: BLE001
            raise ImageGenError(f"undecodable image: {exc}"[:200]) from exc
        if min(im.size) < MIN_EDGE:
            raise ImageGenError(f"image too small: {im.size}")
        small = im.convert("L").resize((64, 64))
        # A flat pastel sweep of a dozen levels scores ~0.6; a solid colour or
        # an all-black failure scores 0 (JPEG noise aside). The line sits
        # between them, on the failure's side of any real backdrop.
        if ImageStat.Stat(small).stddev[0] < 0.25:
            raise BlankImageError("near-uniform image (a blank or a solid colour)")
        fmt = (im.format or "").lower()
        if fmt == "jpeg":
            return data, "image/jpeg"
        if fmt == "png":
            return data, "image/png"
        # webp and friends: the compositor and R2 lifecycle are set up for jpeg/png.
        buf = io.BytesIO()
        im.convert("RGB").save(buf, format="JPEG", quality=92, subsampling=0)
        return buf.getvalue(), "image/jpeg"


def _snap16(n: int) -> int:
    """BFL requires multiples of 16; round to the nearest so the crop is tiny."""
    return max(64, int(round(n / 16.0)) * 16)


# --------------------------------------------------------------------------- #
# fal.ai
# --------------------------------------------------------------------------- #
class FalProvider(HttpImageProvider):
    """fal.ai synchronous endpoint.

    Contract (fal.ai/models/fal-ai/flux/schnell/api, docs/model-endpoints):
      POST https://fal.run/{model}         Authorization: Key $FAL_KEY
      body   {prompt, image_size: {width, height}, num_inference_steps, seed,
              num_images, enable_safety_checker, output_format}
      reply  {images: [{url, width, height, content_type}], seed, has_nsfw_concepts}
             the request id travels in the x-fal-request-id response header
    FLUX.1 [schnell] is $0.003 per megapixel there; a 1080x1350 post is 1.46MP.
    """

    name = "fal"
    BASE = "https://fal.run"

    def __init__(self) -> None:
        self.model = settings.imagegen_fal_model or "fal-ai/flux/schnell"
        self.cost_micros_per_image = _cost_for(self.name)

    def _check_key(self) -> None:
        if not settings.fal_key:
            raise ImageGenError("FAL_KEY is unset")

    def _payload(self, req: ImageRequest) -> dict[str, Any]:
        body: dict[str, Any] = {
            "prompt": flux_prompt(req.prompt, req.negative),
            "image_size": {"width": req.width, "height": req.height},
            "num_images": 1,
            "enable_safety_checker": True,
            "output_format": "jpeg",
        }
        if "schnell" in self.model:
            body["num_inference_steps"] = 4
        if req.seed is not None:
            body["seed"] = req.seed
        return body

    async def _submit(self, client: httpx.AsyncClient, req: ImageRequest) -> dict[str, Any]:
        r = await client.post(
            f"{self.BASE}/{self.model}",
            headers={"Authorization": f"Key {settings.fal_key}"},
            json=self._payload(req),
        )
        r.raise_for_status()
        body = self._json(r, "fal")
        body["_request_id"] = r.headers.get("x-fal-request-id") or body.get("request_id")
        return body

    async def _wait(
        self, client: httpx.AsyncClient, job: dict[str, Any], req: ImageRequest
    ) -> dict[str, Any]:
        # The synchronous endpoint already waited; the "job" is the reply.
        images = job.get("images") or []
        if not images or not isinstance(images[0], dict) or not images[0].get("url"):
            raise ImageGenError(f"fal: no image in response: {str(job)[:200]}")
        if (job.get("has_nsfw_concepts") or [False])[0]:
            raise ImageGenError("fal: safety checker flagged the picture")
        return {
            "url": images[0]["url"],
            "mime": images[0].get("content_type"),
            "job_id": job.get("_request_id"),
            "seed": job.get("seed", req.seed),
            "raw": {"model": self.model, "timings": job.get("timings")},
        }


# --------------------------------------------------------------------------- #
# Replicate
# --------------------------------------------------------------------------- #
# The official flux-schnell model takes an aspect ratio, not pixels
# (replicate/cog-flux ASPECT_RATIOS); the nearest allowed ratio is chosen and
# the compositor's object-fit covers the remainder. 4:5 renders 896x1088.
REPLICATE_RATIOS = {
    "1:1": 1.0,
    "16:9": 16 / 9,
    "21:9": 21 / 9,
    "3:2": 1.5,
    "2:3": 2 / 3,
    "4:5": 0.8,
    "5:4": 1.25,
    "3:4": 0.75,
    "4:3": 4 / 3,
    "9:16": 9 / 16,
    "9:21": 9 / 21,
}


def nearest_ratio(width: int, height: int) -> str:
    target = width / max(1, height)
    return min(REPLICATE_RATIOS, key=lambda k: abs(REPLICATE_RATIOS[k] - target))


class ReplicateProvider(HttpImageProvider):
    """Replicate official-model predictions, synchronous where possible.

    Contract (replicate.com/docs/reference/http, .../topics/predictions):
      POST https://api.replicate.com/v1/models/{owner}/{name}/predictions
           Authorization: Bearer $REPLICATE_API_TOKEN   Prefer: wait=60
      body  {input: {prompt, aspect_ratio, megapixels, num_outputs,
                     output_format, output_quality, go_fast, seed}}
      reply {id, status: starting|processing|succeeded|failed|canceled,
             output: [url], error, urls: {get}}
      GET  https://api.replicate.com/v1/predictions/{id}   (until terminal)
    black-forest-labs/flux-schnell is "$3.00 / thousand output images".
    """

    name = "replicate"
    BASE = "https://api.replicate.com/v1"
    POLL_S = 2.0
    POLL_TIMEOUT_S = 120.0
    TERMINAL = ("succeeded", "failed", "canceled")

    def __init__(self) -> None:
        self.model = settings.imagegen_replicate_model or "black-forest-labs/flux-schnell"
        self.cost_micros_per_image = _cost_for(self.name)

    def _check_key(self) -> None:
        if not settings.replicate_api_token:
            raise ImageGenError("REPLICATE_API_TOKEN is unset")

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {settings.replicate_api_token}",
            "Prefer": "wait=60",
            "Content-Type": "application/json",
        }

    def _input(self, req: ImageRequest) -> dict[str, Any]:
        body: dict[str, Any] = {
            "prompt": flux_prompt(req.prompt, req.negative),
            "aspect_ratio": nearest_ratio(req.width, req.height),
            "megapixels": "1",
            "num_outputs": 1,
            "output_format": "jpg",
            "output_quality": 92,
            "go_fast": True,
        }
        if "schnell" in self.model:
            body["num_inference_steps"] = 4
        if req.seed is not None:
            body["seed"] = req.seed
        return body

    async def _submit(self, client: httpx.AsyncClient, req: ImageRequest) -> dict[str, Any]:
        r = await client.post(
            f"{self.BASE}/models/{self.model}/predictions",
            headers=self._headers(),
            json={"input": self._input(req)},
        )
        r.raise_for_status()
        return self._json(r, "replicate")

    async def _wait(
        self, client: httpx.AsyncClient, job: dict[str, Any], req: ImageRequest
    ) -> dict[str, Any]:
        pred = job
        deadline = time.monotonic() + self.POLL_TIMEOUT_S
        while pred.get("status") not in self.TERMINAL:
            if time.monotonic() > deadline:
                raise ImageGenError(f"replicate: prediction {pred.get('id')} not done in time")
            await asyncio.sleep(self.POLL_S)
            get_url = (pred.get("urls") or {}).get("get") or f"{self.BASE}/predictions/{pred['id']}"
            pred = self._json(await self._get(client, get_url, self._headers()), "replicate")
        if pred.get("status") != "succeeded":
            raise ImageGenError(f"replicate: {pred.get('status')}: {str(pred.get('error'))[:300]}")
        out = pred.get("output")
        url = out[0] if isinstance(out, list) and out else out if isinstance(out, str) else None
        if not url:
            raise ImageGenError("replicate: succeeded with no output")
        return {
            "url": url,
            "mime": "image/jpeg",
            "job_id": pred.get("id"),
            "seed": req.seed,
            "raw": {"model": self.model, "metrics": pred.get("metrics")},
        }


# --------------------------------------------------------------------------- #
# Black Forest Labs direct
# --------------------------------------------------------------------------- #
class BflProvider(HttpImageProvider):
    """BFL's own API: submit, then poll.

    Contract (github.com/black-forest-labs/skills, bfl-api references):
      POST https://api.bfl.ai/v1/{model}     x-key: $BFL_API_KEY
      body  {prompt, width, height (multiples of 16, <=4MP), seed,
             safety_tolerance, output_format}
      reply {id, polling_url}
      GET   polling_url  -> {status: Pending|Ready|Error, result: {sample: url}}
      The sample URL expires in 10 minutes; it is downloaded at once.
    FLUX.2 [klein] 4B is 1.4c for the first megapixel; [pro] 3c.
    """

    name = "bfl"
    BASE = "https://api.bfl.ai/v1"
    POLL_S = 1.5
    POLL_TIMEOUT_S = 120.0
    FAILED = ("Error", "Failed", "Content Moderated", "Request Moderated", "Task not found")

    def __init__(self) -> None:
        self.model = settings.imagegen_bfl_model or "flux-2-klein-4b"
        self.cost_micros_per_image = _cost_for(self.name)

    def _check_key(self) -> None:
        if not settings.bfl_api_key:
            raise ImageGenError("BFL_API_KEY is unset")

    def _headers(self) -> dict[str, str]:
        return {"x-key": settings.bfl_api_key, "Content-Type": "application/json"}

    def _payload(self, req: ImageRequest) -> dict[str, Any]:
        w, h = _snap16(req.width), _snap16(req.height)
        while w * h > 4_000_000:  # the documented ceiling
            w, h = _snap16(int(w * 0.9)), _snap16(int(h * 0.9))
        body: dict[str, Any] = {
            "prompt": flux_prompt(req.prompt, req.negative),
            "width": w,
            "height": h,
            "safety_tolerance": 2,
            "output_format": "jpeg",
        }
        if req.seed is not None:
            body["seed"] = req.seed
        return body

    async def _submit(self, client: httpx.AsyncClient, req: ImageRequest) -> dict[str, Any]:
        r = await client.post(
            f"{self.BASE}/{self.model}", headers=self._headers(), json=self._payload(req)
        )
        r.raise_for_status()
        sub = self._json(r, "bfl")
        if not sub.get("polling_url"):
            raise ImageGenError(f"bfl: no polling_url: {str(sub)[:200]}")
        return sub

    async def _wait(
        self, client: httpx.AsyncClient, job: dict[str, Any], req: ImageRequest
    ) -> dict[str, Any]:
        polling_url = job["polling_url"]
        if not polling_url.startswith("https://"):
            raise ImageGenError("bfl: refusing non-https polling url")
        deadline = time.monotonic() + self.POLL_TIMEOUT_S
        delay = self.POLL_S
        while True:
            await asyncio.sleep(delay)
            delay = min(delay * 1.5, self.POLL_S * 3)
            st = self._json(await self._get(client, polling_url, self._headers()), "bfl")
            status = st.get("status")
            if status == "Ready":
                result = st.get("result") or {}
                url = result.get("sample")
                if not url:
                    raise ImageGenError("bfl: Ready with no sample")
                return {
                    "url": url,
                    "mime": "image/jpeg",
                    "job_id": job.get("id"),
                    "seed": result.get("seed", req.seed),
                    "raw": {"model": self.model},
                }
            if status in self.FAILED:
                raise ImageGenError(f"bfl: {status}: {str(st.get('error') or st)[:300]}")
            if time.monotonic() > deadline:
                raise ImageGenError(f"bfl: {job.get('id')} still {status} after timeout")


# --------------------------------------------------------------------------- #
# OpenAI (gpt-image-1 / dall-e-3)
# --------------------------------------------------------------------------- #
# gpt-image-1 renders only a fixed set of sizes, so the nearest one to the
# brief's aspect is chosen and the compositor's object-fit covers the rest
# (the same approach as Replicate's aspect ratios). OpenAI takes no negative
# prompt, so flux_prompt() folds the brief's negative into positive phrasing.
OPENAI_SIZES = {
    "1024x1024": 1.0,  # square
    "1024x1536": 1024 / 1536,  # portrait 2:3
    "1536x1024": 1536 / 1024,  # landscape 3:2
}


def nearest_openai_size(width: int, height: int) -> str:
    target = width / max(1, height)
    return min(OPENAI_SIZES, key=lambda k: abs(OPENAI_SIZES[k] - target))


class OpenAIProvider(HttpImageProvider):
    """OpenAI Images API, synchronous.

    Contract (platform.openai.com/docs/api-reference/images/create):
      POST https://api.openai.com/v1/images/generations
           Authorization: Bearer $OPENAI_API_KEY
      body   {model, prompt, size, n, output_format, quality}
      reply  {data: [{b64_json}]}   -- gpt-image-1 always returns base64;
             dall-e-3 returns {data: [{url}]} unless b64 is requested.
    gpt-image-1 renders a fixed set of sizes; the nearest to the brief's aspect
    is used and the compositor covers the remainder. It has no negative prompt.
    """

    name = "openai"
    BASE = "https://api.openai.com/v1"
    # gpt-image-1 holds the request open while it renders, which is slower than
    # FLUX; give the read more room but stay inside the shared BUDGET_S cap.
    TIMEOUT = httpx.Timeout(connect=10.0, read=150.0, write=30.0, pool=10.0)

    def __init__(self) -> None:
        self.model = settings.imagegen_openai_model or "gpt-image-1"
        self.cost_micros_per_image = _cost_for(self.name)

    def _check_key(self) -> None:
        if not settings.openai_api_key:
            raise ImageGenError("OPENAI_API_KEY is unset")

    def _payload(self, req: ImageRequest) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "prompt": flux_prompt(req.prompt, req.negative),
            "size": nearest_openai_size(req.width, req.height),
            "n": 1,
        }
        # gpt-image-1 accepts output_format + quality; dall-e-3 does not, and
        # returns a URL rather than base64 unless asked.
        if "gpt-image" in self.model:
            body["output_format"] = "jpeg"
            body["quality"] = settings.imagegen_openai_quality or "medium"
        else:
            body["response_format"] = "b64_json"
        return body

    async def _submit(self, client: httpx.AsyncClient, req: ImageRequest) -> dict[str, Any]:
        r = await client.post(
            f"{self.BASE}/images/generations",
            headers={
                "Authorization": f"Bearer {settings.openai_api_key}",
                "Content-Type": "application/json",
            },
            json=self._payload(req),
        )
        r.raise_for_status()
        return self._json(r, "openai")

    async def _wait(
        self, client: httpx.AsyncClient, job: dict[str, Any], req: ImageRequest
    ) -> dict[str, Any]:
        # The endpoint is synchronous; the "job" is already the reply.
        data = job.get("data") or []
        if not data or not isinstance(data[0], dict):
            raise ImageGenError(f"openai: no image in response: {str(job)[:200]}")
        first = data[0]
        b64 = first.get("b64_json")
        if b64:
            return {
                "data": base64.b64decode(b64),
                "mime": "image/jpeg" if "gpt-image" in self.model else "image/png",
                "job_id": None,
                "seed": req.seed,
                "raw": {"model": self.model},
            }
        url = first.get("url")
        if url:  # dall-e-3 default shape
            return {
                "url": url,
                "mime": "image/png",
                "job_id": None,
                "seed": req.seed,
                "raw": {"model": self.model},
            }
        raise ImageGenError(f"openai: no b64_json or url in response: {str(first)[:200]}")


REGISTRY = {
    "mock": MockImageProvider,
    "fal": FalProvider,
    "replicate": ReplicateProvider,
    "bfl": BflProvider,
    "openai": OpenAIProvider,
}


def get_provider(name: str | None = None):
    return REGISTRY[name or settings.imagegen_provider]()
