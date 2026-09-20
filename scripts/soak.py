"""The numbers nobody has: real latency, real rejection rate, real cost.

Everything about this pipeline that matters commercially -- how long a creative
takes, how often the background gate throws a picture away, what a delivered
slide actually costs -- has only ever been estimated. This runs N real
generations through the REAL vendor, the REAL inspector and the REAL compositor
and prints the measured figures. It needs no database, no queue and no
WhatsApp: it is the generate -> inspect -> compose -> export path and nothing else.

    IMAGEGEN_PROVIDER=openai OPENAI_API_KEY=... \\
    ANTHROPIC_API_KEY=... ANTHROPIC_MODEL=... \\
    python scripts/soak.py --singles 8 --carousels 2 --slides 6

IT SPENDS REAL MONEY: about $0.29 per generated picture at the production
settings, more when the gate rejects. The default run (8 singles + 2x6 slides
= 20 pictures) is ~$6-9. It prints the estimate and asks before it starts;
--yes skips the question.

Outputs land in out/soak/: the delivered pictures, report.json and records.json.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402
from app.creative import bggate, compose, pipeline, shotplan  # noqa: E402
from app.creative.brief import CreativeBrief  # noqa: E402
from app.creative.imagegen import get_provider  # noqa: E402
from app.creative.imagegen.base import generation_size  # noqa: E402
from app.telemetry.stages import Trace  # noqa: E402

OUT = ROOT / "out" / "soak"

# Deliberately varied: categories, surfaces, people, and the subjects most
# likely to tempt a model into lettering (packaging, shopfronts, menus).
SUBJECTS = [
    (
        "food",
        "a glass bottle of golden cold-pressed coconut oil on a worn wooden table, fresh "
        "coconut halves beside it, soft morning window light, empty space above",
    ),
    (
        "bakery",
        "a tray of fresh butter croissants on a marble counter in a small bakery, flour "
        "dust in the air, warm light, room for copy at the top",
    ),
    (
        "jewellery",
        "a pair of gold jhumka earrings resting on dark green velvet, one hard key "
        "light, shallow depth of field, clean space on the left",
    ),
    (
        "skincare",
        "an unbranded amber serum bottle on pale stone with a single green leaf and "
        "water droplets, diffused light, generous negative space",
    ),
    (
        "restaurant",
        "a steel thali with dal, rice, sabzi and roti on a banana leaf, seen from "
        "three-quarter height, steam rising, warm side light",
    ),
    (
        "boutique",
        "a folded handloom cotton saree in indigo and mustard on a wooden bench, "
        "natural window light, weave visible, calm background",
    ),
    (
        "cafe",
        "a flat white in a ceramic cup on a small round cafe table by a window, a "
        "plain notebook beside it, morning light, soft background",
    ),
    (
        "fitness",
        "a person tying their running shoes on a gym floor, seen from low down, "
        "available light, plain wall behind, candid",
    ),
]

BRAND = types.SimpleNamespace(
    name="Soak Test Brand", category="food", logo_url=None, logo_src=None, logo_analysis={},
    palette={"primary": "#123B2E", "secondary": "#FFFFFF", "accent": "#E4572E", "ink": "#FFFFFF"},
    fonts={"heading": "Poppins", "body": "Inter"}, never_say=[],
    template_prefs={"signature": "none", "look": "warm"},
)  # fmt: skip


def _brief(kind: str, slides: int, offset: int) -> CreativeBrief:
    def vd(i):
        return {"prompt": SUBJECTS[(offset + i) % len(SUBJECTS)][1]}

    if kind == "single":
        return CreativeBrief.model_validate(
            {"intent": "promo", "format": {"type": "single"}, "headline": "Weekend Sale",
             "subhead": "Fresh this week", "cta": "Order on WhatsApp",
             "template_id": "lower_third", "visual_direction": vd(0)}
        )  # fmt: skip
    return CreativeBrief.model_validate(
        {"intent": "educational", "format": {"type": "carousel", "slide_count": slides},
         "headline": "Six things we make fresh", "cta": "Save this post",
         "template_id": "lower_third", "visual_direction": vd(0),
         "slides": [{"position": i + 1, "headline": f"Made fresh, number {i + 1}",
                     "visual_direction": vd(i)} for i in range(slides)]}
    )  # fmt: skip


def _pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))] if xs else 0.0


def _stats(xs: list[float]) -> dict:
    if not xs:
        return {"n": 0}
    return {"n": len(xs), "p50": round(statistics.median(xs), 1), "p95": round(_pct(xs, 0.95), 1),
            "max": round(max(xs), 1)}  # fmt: skip


async def _one_slide(brief, slide, provider, register, records, job: str) -> None:
    from app.creative import photoreal

    trace = Trace(f"{job}-s{slide.position}")
    ctx = types.SimpleNamespace(trace=trace)
    prompt, negative = photoreal.photographic(
        slide.visual_direction.prompt, slide.visual_direction.negative_prompt,
        category=BRAND.category, position=slide.position,
        slide_count=len(brief.slides) if brief.is_carousel() else 1, palette=BRAND.palette,
        style=shotplan.style_of(BRAND.template_prefs, BRAND.name),
    )  # fmt: skip
    rec = {"job": job, "slide": slide.position, "ok": False}
    t0 = time.perf_counter()
    try:
        res, cost, rejections = await pipeline._generate_checked(
            ctx, provider, brief, slide, f"{job}-{slide.position}", prompt, negative,
            generation_size(brief.pixel_size()), register, f"slide{slide.position}",
        )  # fmt: skip
        t1 = time.perf_counter()
        png = await compose.compose(brief, slide, BRAND, res.data, res.mime)
        jpg = compose.export_jpeg(png, brief.pixel_size())
        (OUT / f"{job}-{slide.position}.jpg").write_bytes(jpg)
        rec.update(
            ok=True,
            cost_micros=cost,
            attempts=len(rejections) + 1,
            rejections=rejections,
            compose_s=round(time.perf_counter() - t1, 2),
            usage=(res.raw or {}).get("usage"),
        )
    except pipeline.BackgroundRejected as exc:
        rec.update(error="gate_exhausted", cost_micros=exc.cost_micros,
                   attempts=len(exc.rejections), rejections=exc.rejections)  # fmt: skip
    except Exception as exc:  # noqa: BLE001 - a soak run reports failures, it does not stop on them
        rec.update(error=f"{type(exc).__name__}: {exc}"[:300])
    rec["total_s"] = round(time.perf_counter() - t0, 2)
    rec["stages_ms"] = dict(trace.timings)
    records.append(rec)
    print(f"  {job} slide {slide.position}: {'ok' if rec['ok'] else rec.get('error')} "
          f"{rec['total_s']}s attempts={rec.get('attempts', '-')} "
          f"${(rec.get('cost_micros') or 0) / 1e6:.3f}")  # fmt: skip


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--singles", type=int, default=8)
    ap.add_argument("--carousels", type=int, default=2)
    ap.add_argument("--slides", type=int, default=6)
    ap.add_argument("--yes", action="store_true", help="do not ask before spending")
    args = ap.parse_args()

    provider = get_provider()
    pictures = args.singles + args.carousels * args.slides
    each = provider.cost_micros_per_image / 1e6
    print(
        f"provider={provider.name} model={getattr(provider, 'model', '-')} "
        f"size={settings.imagegen_size} inspector={'on' if bggate.available() else 'MISSING'}"
    )
    print(
        f"{pictures} pictures x ${each:.3f} = ${pictures * each:.2f} before gate retries "
        f"(cap ${settings.imagegen_gate_budget_micros / 1e6:.2f}/slide)"
    )
    if provider.name == "mock":
        print("IMAGEGEN_PROVIDER is mock: this would measure nothing. Set it to a real vendor.")
        return 2
    if not bggate.available():
        print(
            "No ANTHROPIC_API_KEY/ANTHROPIC_MODEL: the gate fails closed, every slide would fail."
        )
        return 2
    if not args.yes and input("Spend this? [y/N] ").strip().lower() != "y":
        return 1

    OUT.mkdir(parents=True, exist_ok=True)
    records: list[dict] = []
    jobs: list[dict] = []
    plan = [("single", 1)] * args.singles + [("carousel", args.slides)] * args.carousels
    for n, (kind, slides) in enumerate(plan, start=1):
        brief = _brief(kind, slides, n)
        if brief.is_carousel():
            shotplan.apply(brief)
        register, job = pipeline.dedupe_register(), f"{kind}{n}"
        print(f"{job}: {slides} slide(s)")
        t0 = time.perf_counter()
        await asyncio.gather(
            *(_one_slide(brief, s, provider, register, records, job) for s in brief.units())
        )
        jobs.append({"job": job, "kind": kind, "slides": slides,
                     "wall_s": round(time.perf_counter() - t0, 1)})  # fmt: skip
    await compose.shutdown()

    ok = [r for r in records if r["ok"]]
    gen_ms = [ms for r in records for k, ms in r["stages_ms"].items() if ":imagegen" in k]
    inspect_ms = [ms for r in records for k, ms in r["stages_ms"].items() if ":inspect" in k]
    reasons: dict[str, int] = {}
    for r in records:
        for rej in r.get("rejections") or []:
            for reason in rej["reasons"]:
                reasons[reason] = reasons.get(reason, 0) + 1
    calls = sum(r.get("attempts") or 0 for r in records)
    spend = sum(r.get("cost_micros") or 0 for r in records) / 1e6
    report = {
        "settings": {"provider": provider.name, "model": getattr(provider, "model", None),
                     "size": settings.imagegen_size, "concurrency": settings.imagegen_concurrency},
        "pictures_requested": len(records), "delivered": len(ok), "failed": len(records) - len(ok),
        "vendor_calls": calls,
        "gate": {"rejections": sum(reasons.values()), "by_reason": reasons,
                 "first_try_pass_rate": round(
                     sum(1 for r in ok if r["attempts"] == 1) / max(1, len(records)), 3)},
        "latency_s": {
            "imagegen_per_call": _stats([ms / 1000 for ms in gen_ms]),
            "inspect_per_call": _stats([ms / 1000 for ms in inspect_ms]),
            "compose_per_slide": _stats([r["compose_s"] for r in ok]),
            "single_post_end_to_end": _stats([j["wall_s"] for j in jobs if j["kind"] == "single"]),
            "carousel_end_to_end": _stats([j["wall_s"] for j in jobs if j["kind"] == "carousel"]),
        },
        "cost_usd": {"total": round(spend, 3),
                     "per_vendor_call": round(spend / max(1, calls), 4),
                     "per_DELIVERED_slide": round(spend / max(1, len(ok)), 4),
                     "note": "price a credit from per_DELIVERED_slide, not from the list price"},
        "errors": [r for r in records if not r["ok"]],
    }  # fmt: skip
    (OUT / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (OUT / "records.json").write_text(json.dumps(records, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "errors"}, indent=2))
    print(f"\npictures and report.json -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
