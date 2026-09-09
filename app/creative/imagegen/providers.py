"""Image-generation adapters.

Only backgrounds come out of here -- never text. Two candidates sit behind one
interface so you can A/B them on the same briefs and switch with an env var;
`HttpImageProvider` holds the shared retry/timing plumbing and each candidate
supplies its own request/response mapping.

Fill in ENDPOINT and the two mapping methods for whichever vendors you picked.
They are left explicit rather than guessed because a wrong payload shape here
fails silently as a blank background.
"""

from __future__ import annotations

import base64
import io
import time
from typing import Any

import httpx

from app.config import settings
from app.creative.imagegen.base import ImageRequest, ImageResult
from app.logging import get_logger

log = get_logger(__name__)


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
        return ImageResult(
            data=buf.getvalue(), mime="image/jpeg", provider=self.name, latency_ms=1
        )


class HttpImageProvider:
    """Shared plumbing. Subclasses define ENDPOINT, _payload and _extract."""

    name = "http"
    cost_micros_per_image = 0
    ENDPOINT = ""
    TIMEOUT = 180

    def _headers(self) -> dict[str, str]:
        raise NotImplementedError

    def _payload(self, req: ImageRequest) -> dict[str, Any]:
        raise NotImplementedError

    def _extract(self, body: dict[str, Any]) -> tuple[bytes, str, str | None]:
        """Return (image_bytes, mime, job_id)."""
        raise NotImplementedError

    async def generate(self, req: ImageRequest) -> ImageResult:
        t0 = time.perf_counter()
        async with httpx.AsyncClient(timeout=self.TIMEOUT) as c:
            r = await c.post(self.ENDPOINT, headers=self._headers(), json=self._payload(req))
            r.raise_for_status()
            body = r.json()
        data, mime, job_id = self._extract(body)
        ms = int((time.perf_counter() - t0) * 1000)
        log.info("imagegen_ok", provider=self.name, ms=ms, bytes=len(data))
        return ImageResult(
            data=data,
            mime=mime,
            provider=self.name,
            job_id=job_id,
            cost_micros=self.cost_micros_per_image,
            latency_ms=ms,
            seed=req.seed,
        )

    @staticmethod
    def _b64(s: str) -> bytes:
        return base64.b64decode(s.split(",", 1)[-1])


class ProviderA(HttpImageProvider):
    """Candidate A -- FILL IN.

    Set ENDPOINT, auth header, and the three mappings. Typical shapes:
      payload:  {"prompt": ..., "negative_prompt": ..., "width": ..., "height": ...}
      response: {"images": ["<base64>"], "id": "..."}   or  {"output": ["<url>"]}
    """

    name = "provider_a"
    cost_micros_per_image = 4000  # 0.4 cents -- correct this from the vendor's pricing
    ENDPOINT = ""  # TODO

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {settings.imagegen_a_api_key}"}

    def _payload(self, req: ImageRequest) -> dict[str, Any]:
        return {
            "prompt": req.prompt,
            "negative_prompt": req.negative,
            "width": req.width,
            "height": req.height,
            "seed": req.seed,
        }

    def _extract(self, body: dict[str, Any]) -> tuple[bytes, str, str | None]:
        return self._b64(body["images"][0]), "image/png", body.get("id")


class ProviderB(HttpImageProvider):
    """Candidate B -- FILL IN. Same contract, different vendor."""

    name = "provider_b"
    cost_micros_per_image = 3000
    ENDPOINT = ""  # TODO

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {settings.imagegen_b_api_key}"}

    def _payload(self, req: ImageRequest) -> dict[str, Any]:
        return {
            "prompt": req.prompt,
            "negative": req.negative,
            "size": f"{req.width}x{req.height}",
            "seed": req.seed,
        }

    def _extract(self, body: dict[str, Any]) -> tuple[bytes, str, str | None]:
        return self._b64(body["data"][0]["b64_json"]), "image/png", body.get("id")


REGISTRY = {"mock": MockImageProvider, "provider_a": ProviderA, "provider_b": ProviderB}


def get_provider(name: str | None = None):
    return REGISTRY[name or settings.imagegen_provider]()
