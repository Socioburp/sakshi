"""The owner's taste, as a few plain sentences the agent can act on.

Everything here is a count over recorded votes. No model, no embedding, no
inference beyond "they approved this more often than that". Below MIN_VOTES
it says nothing at all: five votes are a hunch, and a hunch presented as a
preference would push the creative the wrong way with confidence.
"""

from __future__ import annotations

import re
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import CreativeEvent
from app.insights.events import DISLIKES_PICTURE, DISLIKES_WORDS, LIKES_PICTURE, LIKES_WORDS

MIN_VOTES = 5
WINDOW_DAYS = 120
_WORD = re.compile(r"[a-z]+")
_NOISE = {"and", "the", "with", "very", "more", "less", "bit", "slightly"}

# Owner-facing names of the templates, for the prompt block.
TEMPLATE_NAMES = {
    "centered_overlay": "big centred headline over the photo",
    "lower_third": "photo on top, words in the lower third",
    "split_card": "photo above a solid brand-colour panel",
    "top_band": "brand-colour band with the words on top, photo below",
    "poster_stack": "poster: big headline top-left over the photo",
    "frame_card": "catalogue card: framed photo, words beneath it",
}


@dataclass
class Taste:
    votes: int = 0
    approval_rate: float | None = None
    template_scores: dict[str, tuple[int, int]] = field(default_factory=dict)  # (likes, dislikes)
    aspect_scores: dict[str, tuple[int, int]] = field(default_factory=dict)
    format_scores: dict[str, tuple[int, int]] = field(default_factory=dict)
    mood_likes: Counter = field(default_factory=Counter)
    mood_dislikes: Counter = field(default_factory=Counter)
    words_rewritten_rate: float | None = None
    photo_like_rate: float | None = None
    active_hours: Counter = field(default_factory=Counter)  # hour of day (UTC) of approvals
    active_weekdays: Counter = field(default_factory=Counter)  # 0 = Monday

    @property
    def enough(self) -> bool:
        return self.votes >= MIN_VOTES

    def best(self, scores: dict[str, tuple[int, int]]) -> str | None:
        ranked = sorted(
            scores.items(), key=lambda kv: (kv[1][0] - kv[1][1], kv[1][0]), reverse=True
        )
        return ranked[0][0] if ranked and ranked[0][1][0] > ranked[0][1][1] else None

    def worst(self, scores: dict[str, tuple[int, int]]) -> str | None:
        ranked = sorted(
            scores.items(), key=lambda kv: (kv[1][1] - kv[1][0], kv[1][1]), reverse=True
        )
        return (
            ranked[0][0]
            if ranked and ranked[0][1][1] >= 2 and ranked[0][1][1] > ranked[0][1][0]
            else None
        )

    def as_prompt_block(self) -> str:
        if not self.enough:
            return ""
        lines = ["## What this owner tends to approve (from their own taps, not their words)"]
        best_t, worst_t = self.best(self.template_scores), self.worst(self.template_scores)
        if best_t:
            lines.append(f"- Layout they approve most: {TEMPLATE_NAMES.get(best_t, best_t)}.")
        if worst_t and worst_t != best_t:
            name = TEMPLATE_NAMES.get(worst_t, worst_t)
            lines.append(f"- Layout they keep sending back: {name} -- avoid unless asked.")
        best_a = self.best(self.aspect_scores)
        if best_a:
            lines.append(f"- Aspect ratio that works for them: {best_a}.")
        best_f = self.best(self.format_scores)
        if best_f:
            lines.append(f"- They approve {best_f} posts more readily than the other kind.")
        liked = [
            m
            for m, _ in self.mood_likes.most_common(3)
            if self.mood_dislikes[m] < self.mood_likes[m]
        ]
        if liked:
            lines.append(f"- Picture moods they approved: {', '.join(liked)}.")
        disliked = [
            m for m, n in self.mood_dislikes.most_common(3) if n >= 2 and n > self.mood_likes[m]
        ]
        if disliked:
            lines.append(f"- Picture moods they rejected: {', '.join(disliked)} -- steer away.")
        if self.words_rewritten_rate is not None and self.words_rewritten_rate >= 0.5:
            lines.append(
                "- They rewrite the words on most creatives: keep copy shorter and plainer, and "
                "ask what they want said before writing a long headline."
            )
        if self.photo_like_rate is not None and self.photo_like_rate >= 0.7:
            lines.append("- Creatives built on their own product photos get approved; prefer them.")
        return "\n".join(lines) if len(lines) > 1 else ""


def _moods(meta: dict) -> list[str]:
    return [
        w for w in _WORD.findall((meta.get("mood") or "").lower()) if len(w) > 2 and w not in _NOISE
    ]


def taste(db: Session, brand_id: uuid.UUID, now: datetime | None = None) -> Taste:
    now = now or datetime.now(UTC)
    rows = db.scalars(
        select(CreativeEvent)
        .where(
            CreativeEvent.brand_id == brand_id,
            CreativeEvent.created_at >= now - timedelta(days=WINDOW_DAYS),
        )
        .order_by(CreativeEvent.created_at.desc())
        .limit(600)
    ).all()
    t = Taste()
    votes = [
        r for r in rows if r.kind in LIKES_PICTURE | DISLIKES_PICTURE | LIKES_WORDS | DISLIKES_WORDS
    ]
    t.votes = len(votes)
    if not votes:
        return t

    def bump(scores: dict, key: str | None, like: bool) -> None:
        if not key:
            return
        likes, dislikes = scores.get(key, (0, 0))
        scores[key] = (likes + 1, dislikes) if like else (likes, dislikes + 1)

    approvals = 0
    words_rewritten = 0
    photo_votes = photo_likes = 0
    # One opinion per brief per axis, newest first: "change the picture" and
    # the regenerate that follows it are the same dislike, not two.
    seen_pic: set[str] = set()
    seen_words: set[str] = set()
    counted = 0
    for r in votes:
        bid = str(r.brief_id) if r.brief_id else None
        m = r.meta or {}
        if r.kind in LIKES_PICTURE or r.kind in DISLIKES_PICTURE:
            if bid is None or bid not in seen_pic:
                like = r.kind in LIKES_PICTURE
                bump(t.template_scores, m.get("template"), like)
                bump(t.aspect_scores, m.get("aspect"), like)
                bump(t.format_scores, m.get("format"), like)
                for w in _moods(m):
                    (t.mood_likes if like else t.mood_dislikes)[w] += 1
                if m.get("has_photo"):
                    photo_votes += 1
                    photo_likes += 1 if like else 0
                counted += 1
                if bid:
                    seen_pic.add(bid)
        if r.kind in DISLIKES_WORDS and (bid is None or bid not in seen_words):
            words_rewritten += 1
            counted += 1
            if bid:
                seen_words.add(bid)
        if r.kind in ("approve", "publish"):
            approvals += 1
            t.active_hours[r.created_at.hour] += 1
            t.active_weekdays[r.created_at.weekday()] += 1
    created = sum(1 for r in rows if r.kind == "created") or counted
    t.votes = counted
    t.approval_rate = min(1.0, approvals / max(1, created))
    t.words_rewritten_rate = min(1.0, words_rewritten / max(1, created))
    t.photo_like_rate = (photo_likes / photo_votes) if photo_votes >= 3 else None
    return t
