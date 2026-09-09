"""STT candidates for the bake-off.

The decision here is the one most likely to be wrong if you pick on vendor
benchmarks: published WER is measured on clean read speech in one language.
Sakshi's input is a shop owner talking Hinglish or Kannada over a ceiling fan,
and what matters is whether "Nandini ghee" survives -- not overall WER.
Score product-name recall. See scripts/stt_bakeoff.py.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from app.config import settings
from app.integrations.stt.base import Transcript
from app.logging import get_logger

log = get_logger(__name__)


def _ext_for(mime: str) -> str:
    return {
        "audio/ogg": "ogg",
        "audio/ogg; codecs=opus": "ogg",
        "audio/opus": "opus",
        "audio/mpeg": "mp3",
        "audio/mp4": "m4a",
        "audio/amr": "amr",
        "audio/wav": "wav",
        "audio/x-wav": "wav",
    }.get((mime or "").split(";")[0].strip(), "ogg")


# accounts.locale is stored as BCP-47 ("hi-IN"). Vendors disagree on the shape
# they want, so the conversion lives here, next to each call, not in the caller.
def bare_code(locale: str) -> str:
    """'hi-IN' -> 'hi' (ElevenLabs, Deepgram)."""
    return (locale or "").split("-")[0].lower()


def bcp47_code(locale: str) -> str:
    """'hi' -> 'hi-IN', 'hi-IN' unchanged (Sarvam)."""
    loc = (locale or "").strip()
    return loc if "-" in loc else f"{loc.lower()}-IN"


class MockStt:
    name = "mock"
    canned = "Kal se weekend sale hai, coconut oil 500 ml two forty nine rupees"

    async def transcribe(
        self, audio: bytes, mime: str, hint_languages: list[str] | None = None
    ) -> Transcript:
        return Transcript(text=self.canned, provider=self.name, language="hi", confidence=0.9)


class ElevenLabsStt:
    """Scribe. Strong multilingual, handles code-switching well in practice."""

    name = "elevenlabs"
    URL = "https://api.elevenlabs.io/v1/speech-to-text"

    async def transcribe(
        self, audio: bytes, mime: str, hint_languages: list[str] | None = None
    ) -> Transcript:
        t0 = time.perf_counter()
        files = {"file": (f"audio.{_ext_for(mime)}", audio, mime or "audio/ogg")}
        data: dict[str, Any] = {"model_id": "scribe_v1", "diarize": "false"}
        if hint_languages and len(hint_languages) == 1:
            data["language_code"] = bare_code(hint_languages[0])
        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.post(
                self.URL,
                headers={"xi-api-key": settings.elevenlabs_api_key},
                files=files,
                data=data,
            )
            r.raise_for_status()
            j = r.json()
        return Transcript(
            text=(j.get("text") or "").strip(),
            provider=self.name,
            language=j.get("language_code"),
            confidence=j.get("language_probability"),
            latency_ms=int((time.perf_counter() - t0) * 1000),
            raw=j,
        )


class DeepgramStt:
    """Nova. Fastest of the three; weakest on Indic proper nouns in our testing plan."""

    name = "deepgram"
    URL = "https://api.deepgram.com/v1/listen"

    async def transcribe(
        self, audio: bytes, mime: str, hint_languages: list[str] | None = None
    ) -> Transcript:
        t0 = time.perf_counter()
        params = {"model": "nova-2", "smart_format": "true", "punctuate": "true"}
        if hint_languages:
            params["language"] = bare_code(hint_languages[0])
        else:
            params["detect_language"] = "true"
        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.post(
                self.URL,
                params=params,
                headers={
                    "Authorization": f"Token {settings.deepgram_api_key}",
                    "Content-Type": mime or "audio/ogg",
                },
                content=audio,
            )
            r.raise_for_status()
            j = r.json()
        alt = j["results"]["channels"][0]["alternatives"][0]
        return Transcript(
            text=alt.get("transcript", "").strip(),
            provider=self.name,
            language=j["results"]["channels"][0].get("detected_language"),
            confidence=alt.get("confidence"),
            latency_ms=int((time.perf_counter() - t0) * 1000),
            raw=j,
        )


class SarvamStt:
    """Indic-first. The one to beat on Kannada, Tamil, Marathi and code-mixed Hindi."""

    name = "sarvam"
    URL = "https://api.sarvam.ai/speech-to-text"

    async def transcribe(
        self, audio: bytes, mime: str, hint_languages: list[str] | None = None
    ) -> Transcript:
        t0 = time.perf_counter()
        lang = bcp47_code(hint_languages[0]) if hint_languages else "unknown"
        files = {"file": (f"audio.{_ext_for(mime)}", audio, mime or "audio/ogg")}
        data = {"model": "saarika:v2", "language_code": lang}
        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.post(
                self.URL,
                headers={"api-subscription-key": settings.sarvam_api_key},
                files=files,
                data=data,
            )
            r.raise_for_status()
            j = r.json()
        return Transcript(
            text=(j.get("transcript") or "").strip(),
            provider=self.name,
            language=j.get("language_code"),
            latency_ms=int((time.perf_counter() - t0) * 1000),
            raw=j,
        )


REGISTRY = {
    "mock": MockStt,
    "elevenlabs": ElevenLabsStt,
    "deepgram": DeepgramStt,
    "sarvam": SarvamStt,
}


def get_stt(name: str | None = None):
    return REGISTRY[name or settings.stt_provider]()


async def transcribe(
    audio: bytes, mime: str, hint_languages: list[str] | None = None, provider: str | None = None
) -> Transcript:
    stt = get_stt(provider)
    try:
        return await stt.transcribe(audio, mime, hint_languages)
    except Exception as exc:  # noqa: BLE001
        log.error("stt_failed", provider=stt.name, error=str(exc))
        raise
