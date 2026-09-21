"""What the bot knew about this brand when it made this creative -- said out loud.

Everything Sakshi knows about a brand was invisible. It retrieved the products
they really sell, the things they had turned down, the layout their approved
posts share; it matched their own photograph to the copy; it handed their exact
colours to the image model. And then it delivered a picture with a one-line
"want changes?", exactly as a template app would. An owner comparing two
screenshots could not tell that one of them came from something that knows
their shop.

The understanding is the product, so it has to be SEEN. After a creative, the
agent gets a short list of facts -- each one true of THIS creative, each one
checkable -- and says one of them in a line: "used your kaju katli photo from
last week", "kept it off red, like you asked".

The rule that makes this worth anything: NOTHING HERE IS INFERRED BY A MODEL.
Every fact is derived in code from data the pipeline actually used. A memory
that was retrieved but does not show up in the copy is not claimed. An owner
who catches the bot "remembering" something it made up will never believe it
again, and rightly.
"""

from __future__ import annotations

import re
from typing import Any

MAX_FACTS = 4
# Similarity at which a retrieved rejection is about THIS request (the retrieval
# floor is 0.25; the catalogue lane's own bar for "this is relevant" is 0.45).
REJECTION_RELEVANT = 0.45
_WORD = re.compile(r"[^\W\d_]{4,}", re.UNICODE)  # letters only, any script, 4+ long
# Words that appear in every shop's catalogue and prove nothing on their own.
_COMMON = {
    "with", "from", "this", "that", "your", "fresh", "pure", "best", "special", "offer",
    "price", "pack", "piece", "pieces", "rupees", "only", "free", "order", "whatsapp",
}  # fmt: skip


def _words(text: str) -> set[str]:
    return {w.lower() for w in _WORD.findall(text or "")} - _COMMON


def _copy_of(brief: Any) -> str:
    bits = [brief.headline, brief.subhead, brief.cta, brief.caption.body]
    for s in brief.slides or []:
        bits += [s.headline, s.subhead]
    return " ".join(b for b in bits if b)


def _short(text: str, limit: int = 90) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rsplit(" ", 1)[0] + "…"


def build(
    *,
    brief: Any,
    grounding: Any = None,
    photo_labels: dict[int, str | None] | None = None,
    usual_template: str | None = None,
    palette: dict | None = None,
    generated: bool = False,
) -> list[dict[str, str]]:
    """Facts that are true of this creative, most persuasive first.

    photo_labels   slide position -> label of the owner's OWN photo used there
    usual_template the layout their approved posts share (None until there is
                   a signature to speak of)
    generated      at least one picture came from the image model
    """
    facts: list[dict[str, str]] = []
    copy_words = _words(_copy_of(brief))

    # 1. Their own photograph. The strongest proof there is: it is THEIR product.
    for position, label in sorted((photo_labels or {}).items()):
        what = _short(label or "", 60) or "the product"
        where = f" on slide {position}" if getattr(brief, "slides", None) else ""
        facts.append({"kind": "own_photo", "fact": f"used their own photo of {what}{where}"})
        if len([f for f in facts if f["kind"] == "own_photo"]) == 2:
            break

    hits = getattr(grounding, "hits", None) or {}

    # 2. A thing they turned down, that was in front of the writer this time.
    # Retrieval casts a deliberately wide net for rejections (grounding.py: a
    # missed one costs a rejected creative). Most of what it catches is not
    # ABOUT this request, and "I kept your note about bare feet in mind" on a
    # mithai post is exactly the hollow claim this module must never make. So
    # only a rejection that is genuinely close to the request is spoken.
    for memory, score in (hits.get("rejection") or [])[:1]:
        if score < REJECTION_RELEVANT:
            continue
        facts.append(
            {
                "kind": "avoided",
                "fact": f"kept clear of what they turned down: {_short(memory.content)}",
            }
        )

    # 3. A product they really sell -- claimed ONLY if it is actually in the copy.
    for memory, _score in hits.get("catalog") or []:
        shared = _words(memory.content) & copy_words
        if len(shared) >= 2 or (len(shared) == 1 and len(_words(memory.content)) <= 2):
            facts.append(
                {"kind": "catalogue", "fact": f"used their own catalogue: {_short(memory.content)}"}
            )
            break

    # 4. The layout their approved posts share.
    if usual_template and brief.template_id == usual_template:
        facts.append(
            {"kind": "usual_layout", "fact": "set in the layout their approved posts share"}
        )

    # 5. Their exact colours, given to the photographer (generated pictures only).
    if generated:
        hexes = [
            str((palette or {}).get(k) or "").upper()
            for k in ("primary", "accent")
            if re.fullmatch(r"#[0-9A-Fa-f]{6}", str((palette or {}).get(k) or ""))
        ]
        if hexes:
            facts.append(
                {
                    "kind": "brand_colours",
                    "fact": f"the picture was shot to sit with their colours ({', '.join(hexes)})",
                }
            )
    return facts[:MAX_FACTS]


HINT = (
    "These are things you KNEW about this brand and used in this creative; each is true. "
    "In your reply, say ONE of them in a short natural line in the owner's language -- the "
    'way someone who knows their shop would mention it ("used your kaju katli photo", '
    '"kept it off red like you said"). At most two. Say ONLY what is in this list: never '
    "add a memory that is not here, never mention hex codes or the word 'memory'. Own "
    "photo and things they turned down matter most to them; colours least."
)
