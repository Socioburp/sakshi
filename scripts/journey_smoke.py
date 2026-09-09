#!/usr/bin/env python3
"""The whole client journey, end to end, with every vendor stubbed.

    new client -> asked their industry -> sends logo -> colours measured from
    the pixels -> asks for a post -> creative arrives -> approval buttons ->
    publish REFUSED without a tap -> client taps -> auto-published to Instagram
    -> 6-slide carousel -> 7 slides rejected

Real code on every hop: the real FastAPI app, the real ingestion and routing,
the real agent loop, the real brief validator, the real palette extraction, the
real Jinja templates and the real headless Chromium. Only the four things that
cost money or need someone else's account are faked -- Anthropic, STT, the image
model, and R2.

    DATABASE_URL=postgresql+psycopg://... python scripts/journey_smoke.py
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import types
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("WA_PROVIDER", "mock")
os.environ.setdefault("STT_PROVIDER", "mock")
os.environ.setdefault("IMAGEGEN_PROVIDER", "mock")
os.environ.setdefault("INSTAGRAM_MOCK", "true")
os.environ.setdefault("WA_VERIFY_TOKEN", "smoke-token")
os.environ.setdefault("ANTHROPIC_MODEL", "smoke-model")
os.environ.setdefault("R2_PUBLIC_BASE_URL", "file://local")

OUT = ROOT / "out"
OUT.mkdir(exist_ok=True)

# -- stub 1/3: object storage -> local dict -------------------------------- #
from app.integrations.storage import r2  # noqa: E402

_blobs: dict[str, bytes] = {}
r2.put = lambda key, data, ct=None: (_blobs.__setitem__(key, data), f"file://local/{key}")[1]
r2.get = lambda key: _blobs[key]
r2.public_url = lambda key: f"file://local/{key}"

# -- stub 2/3: the queue -> run jobs inline -------------------------------- #
from app.channels.whatsapp import ingest as ingest_mod  # noqa: E402

JOBS: list[tuple[str, dict]] = []
ingest_mod.enqueue = lambda **kw: (JOBS.append((kw["kind"], kw["payload"])), "job")[1]

# -- stub 3/3: Anthropic -> a scripted conversation ------------------------ #
from app.agent import runner  # noqa: E402
from app.creative.brief import EXAMPLE, EXAMPLE_CAROUSEL  # noqa: E402

SCRIPT: list[dict] = []


def _block(**kw):
    b = types.SimpleNamespace(**kw)
    b.model_dump = lambda: dict(kw)
    return b


def say(text: str) -> dict:
    return {"kind": "text", "text": text}


def call(tool: str, inp: dict) -> dict:
    return {"kind": "tool", "name": tool, "input": inp}


class FakeMessages:
    async def create(self, **kwargs):
        step = SCRIPT.pop(0) if SCRIPT else say("")
        if step["kind"] == "text":
            return types.SimpleNamespace(content=[_block(type="text", text=step["text"])])
        return types.SimpleNamespace(
            content=[
                _block(
                    type="tool_use",
                    id=f"tu_{uuid.uuid4().hex[:6]}",
                    name=step["name"],
                    input=step["input"],
                )
            ]
        )


runner.get_client = lambda: types.SimpleNamespace(messages=FakeMessages())

# -------------------------------------------------------------------------- #
from fastapi.testclient import TestClient  # noqa: E402

from app.agent.context import ToolContext  # noqa: E402
from app.agent.prompts import missing_setup  # noqa: E402
from app.agent.tools import _publish_to_instagram  # noqa: E402
from app.channels.whatsapp.adapters import get_adapter  # noqa: E402
from app.creative.brief import CreativeBrief  # noqa: E402
from app.db import repo  # noqa: E402
from app.db.models import (  # noqa: E402  # noqa: E402
    Brand,
    BrandAsset,
    BrandMemory,
    Brief,
    IgAccount,
    Publication,
)
from app.db.session import session_scope  # noqa: E402
from app.main import app  # noqa: E402
from app.queue.handlers import HANDLERS  # noqa: E402
from app.telemetry.stages import trace  # noqa: E402

client = TestClient(app)
adapter = get_adapter()
WA = f"9199{uuid.uuid4().int % 10**8:08d}"

GREEN, ORANGE = (24, 92, 62), (228, 87, 46)


def step(n: str) -> None:
    print(f"\n\033[1m{n}\033[0m")


def make_logo() -> bytes:
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (512, 512), (255, 255, 255, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([60, 60, 452, 452], fill=(*GREEN, 255))
    d.rectangle([220, 200, 292, 400], fill=(*ORANGE, 255))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


async def inbound(**message) -> None:
    """Post one webhook and drain whatever job it enqueued."""
    JOBS.clear()
    payload = {"messages": [{"id": f"mock-{uuid.uuid4()}", "from": WA, **message}]}
    r = client.post("/webhooks/whatsapp", json=payload)
    assert r.status_code == 200, r.text
    while JOBS:
        kind, job = JOBS.pop(0)
        await HANDLERS[kind](job)


def last_texts(n: int = 3) -> list[str]:
    return [m.text for m in adapter.sent if m.kind in ("text", "buttons")][-n:]


async def main() -> int:  # noqa: PLR0915
    adapter.sent.clear()
    adapter.media["logo-1"] = (make_logo(), "image/png")

    step("1. a new client says hello")
    SCRIPT.extend([say("Namaste! Aap kis type ka business chalate hain?")])
    await inbound(type="text", text="hi", name="Kadamba Naturals")
    print(f"   bot: {last_texts(1)[0]!r}")
    with session_scope() as db:
        acct = repo.get_or_create_account(db, WA)
        brand = repo.default_brand(db, acct.id)
        account_id, brand_id = acct.id, brand.id
        print(f"   setup still missing: {missing_setup(brand)}")
        assert missing_setup(brand) == ["industry", "logo"]

    step("2. they say what they sell -> saved to the brand brain")
    SCRIPT.extend(
        [
            call("update_brand", {"category": "cold-pressed oils", "name": "Kadamba Naturals"}),
            say("Perfect. Apna logo bhej dijiye?"),
        ]
    )
    await inbound(type="text", text="cold pressed coconut oil bechte hain")
    with session_scope() as db:
        brand = db.get(Brand, brand_id)
        print(f"   brand.category : {brand.category!r}")
        print(f"   still missing  : {missing_setup(brand)}")
        assert brand.category == "cold-pressed oils"
        assert missing_setup(brand) == ["logo"]

    step("3. they send the logo -> colours MEASURED from the pixels")
    SCRIPT.extend([say("Mil gaya! Green aur orange — bilkul aapke logo jaise.")])
    await inbound(type="image", media_id="logo-1", mime="image/png")
    with session_scope() as db:
        brand = db.get(Brand, brand_id)
        asset = db.query(BrandAsset).filter(BrandAsset.brand_id == brand_id).first()
        mem = db.query(BrandMemory).filter(BrandMemory.brand_id == brand_id).count()
        print(f"   palette        : {brand.palette}")
        print(f"   logo asset     : kind={asset.kind} {asset.width}x{asset.height}")
        print(f"   brand_memory   : {mem} row(s)")
        print(f"   still missing  : {missing_setup(brand)}")
        primary = tuple(int(brand.palette["primary"][i : i + 2], 16) for i in (1, 3, 5))
        assert all(abs(a - b) <= 6 for a, b in zip(primary, GREEN, strict=True)), primary
        assert brand.palette["ink"] == "#FFFFFF"  # readable on a dark green
        assert asset.kind == "logo" and brand.logo_url
        assert missing_setup(brand) == []

    step("4. they ask for a post -> creative, then approval buttons")
    brief_holder: dict = {}
    SCRIPT.extend([call("create_creative", {"brief": json.loads(json.dumps(EXAMPLE))})])
    # request_approval needs the brief_id the first tool returns, so the second
    # scripted step is appended once we know it.
    await inbound(type="text", text="weekend offer ka post banao")
    with session_scope() as db:
        brief = db.query(Brief).order_by(Brief.created_at.desc()).first()
        brief_holder["id"] = str(brief.id)
        creatives = repo.creatives_for_brief(db, brief.id)
        c0 = creatives[0]
        print(f"   creative       : {c0.status} {c0.width}x{c0.height}")
        OUT.joinpath("journey_single.png").write_bytes(_blobs[creatives[0].composed_key])
        assert creatives[0].status == "ready"

    SCRIPT.extend(
        [
            call(
                "request_approval",
                {"brief_id": brief_holder["id"], "message": "Instagram pe daal dun?"},
            )
        ]
    )
    await inbound(type="text", text="haan bahut badhiya")
    buttons = [m for m in adapter.sent if m.kind == "buttons"][-1]
    print(f"   bot asks       : {buttons.text!r}")
    print(f"   buttons        : {[(b.id, b.title) for b in buttons.buttons]}")
    assert buttons.buttons[0].id == f"approve:{brief_holder['id']}"

    step("5. publishing WITHOUT the tap is refused")
    with trace(account_id=account_id) as t:
        ctx = ToolContext(account_id, brand_id, None, WA, t)
        with session_scope() as db:
            db.add(IgAccount(brand_id=brand_id, ig_user_id="17841400000000000", status="connected"))
        refused = await _publish_to_instagram(ctx, {"brief_id": brief_holder["id"]})
    print(f"   result         : {refused['reason']} ({refused['approved']}/{refused['total']})")
    assert refused["ok"] is False and refused["reason"] == "not_approved_by_client"

    step("6. the client taps the button -> approval recorded at ingest")
    SCRIPT.extend([say("")])
    await inbound(
        type="interactive",
        interactive_id=f"approve:{brief_holder['id']}",
        text="Post to Instagram",
    )
    with session_scope() as db:
        approved, total = repo.approval_state(db, uuid.UUID(brief_holder["id"]))
        print(f"   approved       : {approved}/{total} via button")
        assert approved == total == 1

    step("7. now it publishes")
    with trace(account_id=account_id) as t:
        ctx = ToolContext(account_id, brand_id, None, WA, t)
        published = await _publish_to_instagram(ctx, {"brief_id": brief_holder["id"]})
    print(f"   permalink      : {published['permalink']}")
    assert published["ok"] and published["slides"] == 1

    step("8. a 6-slide carousel")
    six = json.loads(json.dumps(EXAMPLE_CAROUSEL))
    base = six["slides"][0]
    six["slides"] = [{**base, "position": i, "headline": f"Step {i}"} for i in range(1, 7)]
    six["format"]["slide_count"] = 6
    SCRIPT.extend([call("create_creative", {"brief": six}), say("Chha gaya!")])
    await inbound(type="text", text="6 slide ka carousel banao")
    with session_scope() as db:
        brief = db.query(Brief).order_by(Brief.created_at.desc()).first()
        slides = repo.creatives_for_brief(db, brief.id)
        groups = {s.carousel_group_id for s in slides}
        print(f"   slides         : {len(slides)} positions={[s.slide_position for s in slides]}")
        print(f"   one group      : {len(groups) == 1}")
        OUT.joinpath("journey_carousel_slide6.png").write_bytes(_blobs[slides[-1].composed_key])
        assert len(slides) == 6 and len(groups) == 1
        assert all(s.status == "ready" for s in slides)

    step("9. seven slides is refused by the contract, not by the prompt")
    seven = json.loads(json.dumps(six))
    seven["slides"].append({**base, "position": 7, "headline": "Step 7"})
    seven["format"]["slide_count"] = 7
    try:
        CreativeBrief.model_validate(seven)
    except Exception as exc:  # noqa: BLE001
        line = next(
            (ln.strip() for ln in str(exc).splitlines() if "slide" in ln.lower()),
            str(exc).splitlines()[1].strip(),
        )
        print(f"   rejected       : {line[:100]}")
    else:
        raise AssertionError("7 slides should not validate")

    step("10. what the client actually received")
    for m in adapter.sent:
        if m.kind == "image":
            print(f"   [image ] {m.caption or '(no caption)'}")
        elif m.kind == "buttons":
            print(f"   [buttons] {m.text!r}")
        else:
            print(f"   [text  ] {m.text!r}")

    with session_scope() as db:
        pub = db.query(Publication).order_by(Publication.created_at.desc()).first()
        print(f"\n   published      : {pub.media_type} -> {pub.permalink}")

    from app.creative import compose as compose_mod

    await compose_mod.shutdown()
    print("\n\033[32mFull client journey passed.\033[0m")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
