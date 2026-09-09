#!/usr/bin/env python3
"""STT bake-off harness.

Scores the three candidates on PRODUCT-NAME ACCURACY, not overall WER. A
transcript that gets every filler word right and turns "Nandini ghee" into
"Nandhini G" is useless to us; one that garbles half the sentence but nails
the product, the price and the offer is fine, because the agent reconstructs
the rest.

Usage
-----
1. Collect 20-30 real voice notes from actual SMB owners. Cover:
   Hinglish, Kannada, at least one other Indic language, shop background
   noise, a fan, a two-second clip, a ninety-second ramble.
2. Drop the audio in samples/voice_notes/ and fill in manifest.json:

   [
     {
       "file": "kn_oil_shop_01.ogg",
       "language": "kn",
       "reference": "full human transcript here",
       "must_catch": ["Nandini", "ghee", "500 ml", "249"],
       "notes": "ceiling fan, street noise"
     }
   ]

   `must_catch` is the scoring surface: product names, brand names, prices,
   quantities. Everything else is decoration.

3. python scripts/stt_bakeoff.py --providers elevenlabs,deepgram,sarvam
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.integrations.stt.providers import REGISTRY, get_stt  # noqa: E402

SAMPLES = Path(__file__).resolve().parents[1] / "samples" / "voice_notes"
MANIFEST = SAMPLES / "manifest.json"

MIME_BY_EXT = {
    ".ogg": "audio/ogg", ".opus": "audio/ogg", ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4", ".wav": "audio/wav", ".amr": "audio/amr",
}


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).lower()
    s = re.sub(r"[^\w\sऀ-ॿಀ-೿஀-௿]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def caught(term: str, hay: str, threshold: float = 0.85) -> bool:
    """Fuzzy containment: STT spellings of proper nouns vary legitimately."""
    t, h = norm(term), norm(hay)
    if not t:
        return False
    if t in h:
        return True
    words = h.split()
    n = len(t.split())
    for i in range(len(words) - n + 1):
        window = " ".join(words[i : i + n])
        if SequenceMatcher(None, t, window).ratio() >= threshold:
            return True
    return False


def wer(reference: str, hypothesis: str) -> float:
    r, h = norm(reference).split(), norm(hypothesis).split()
    if not r:
        return 0.0
    d = [[0] * (len(h) + 1) for _ in range(len(r) + 1)]
    for i in range(len(r) + 1):
        d[i][0] = i
    for j in range(len(h) + 1):
        d[0][j] = j
    for i in range(1, len(r) + 1):
        for j in range(1, len(h) + 1):
            cost = 0 if r[i - 1] == h[j - 1] else 1
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cost)
    return d[len(r)][len(h)] / len(r)


async def run(providers: list[str], limit: int | None) -> dict:
    if not MANIFEST.exists():
        sys.exit(f"missing {MANIFEST} -- see the docstring in this file")
    manifest = json.loads(MANIFEST.read_text())[:limit]
    results: dict[str, dict] = {p: {"rows": []} for p in providers}

    for entry in manifest:
        path = SAMPLES / entry["file"]
        if not path.exists():
            print(f"  skip (missing): {entry['file']}")
            continue
        audio = path.read_bytes()
        mime = MIME_BY_EXT.get(path.suffix.lower(), "audio/ogg")
        for p in providers:
            try:
                tr = await get_stt(p).transcribe(audio, mime, [entry.get("language", "unknown")])
            except Exception as exc:  # noqa: BLE001
                results[p]["rows"].append(
                    {"file": entry["file"], "error": str(exc)[:200], "term_recall": 0.0}
                )
                continue
            terms = entry.get("must_catch", [])
            hits = [t for t in terms if caught(t, tr.text)]
            results[p]["rows"].append(
                {
                    "file": entry["file"],
                    "language": entry.get("language"),
                    "term_recall": len(hits) / len(terms) if terms else None,
                    "missed": [t for t in terms if t not in hits],
                    "wer": round(wer(entry.get("reference", ""), tr.text), 3)
                    if entry.get("reference")
                    else None,
                    "latency_ms": tr.latency_ms,
                    "transcript": tr.text,
                }
            )
    return results


def summarise(results: dict) -> None:
    print(f"\n{'provider':<14}{'term recall':>13}{'perfect':>10}{'WER':>8}{'p50 ms':>9}")
    print("-" * 54)
    ranked = []
    for p, data in results.items():
        rows = [r for r in data["rows"] if r.get("term_recall") is not None]
        if not rows:
            continue
        recall = sum(r["term_recall"] for r in rows) / len(rows)
        perfect = sum(1 for r in rows if r["term_recall"] == 1.0) / len(rows)
        wers = [r["wer"] for r in rows if r.get("wer") is not None]
        lats = sorted(r.get("latency_ms", 0) for r in rows)
        p50 = lats[len(lats) // 2] if lats else 0
        ranked.append((recall, p))
        print(
            f"{p:<14}{recall:>12.1%}{perfect:>10.0%}"
            f"{(sum(wers)/len(wers) if wers else float('nan')):>8.2f}{p50:>9}"
        )
    if ranked:
        print(f"\nwinner on product-name recall: {max(ranked)[1]}")
    print("\nWorst misses (fix these before shipping, or add them to the brand lexicon):")
    for p, data in results.items():
        misses: dict[str, int] = {}
        for r in data["rows"]:
            for m in r.get("missed", []) or []:
                misses[m] = misses.get(m, 0) + 1
        top = sorted(misses.items(), key=lambda kv: -kv[1])[:5]
        if top:
            print(f"  {p}: " + ", ".join(f"{t} x{n}" for t, n in top))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--providers", default="elevenlabs,deepgram,sarvam")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args()
    providers = [p.strip() for p in args.providers.split(",") if p.strip() in REGISTRY]
    results = asyncio.run(run(providers, args.limit))
    summarise(results)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(results, indent=2, ensure_ascii=False))
        print(f"\nwrote {args.json_out}")


if __name__ == "__main__":
    main()
