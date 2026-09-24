"""What their followers responded to, from Instagram Insights.

The taste profile (profile.py) learns from the owner's taps. This learns
from the people the posts are for. Both are counts, no model: a post's
saves, shares, comments and likes against the accounts it reached, grouped
by the choices that made it -- format, layout, intent, the hour it went up.

Three things read it: the daily idea (a follow-up to the post that beat the
account's usual by half again), the system prompt (a few lines the agent
can act on), and the Monday readout that rides above the week's line-up.

Below MIN_POSTS it says nothing. Three posts are anecdotes, and an
"insight" built on them would push the next post the wrong way with
confidence. Saves and shares weigh more than likes on purpose: they are
what Instagram's own ranking rewards, and what a shop's regulars do when a
post is worth keeping.
"""

from __future__ import annotations

import statistics
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Brand, Brief, Creative, IgAccount, IgAccountStat, PostMetric, Publication
from app.db.session import session_scope
from app.insights import events
from app.insights.profile import TEMPLATE_NAMES
from app.integrations.instagram import client as ig
from app.integrations.instagram import insights
from app.logging import get_logger

log = get_logger(__name__)

IST = ZoneInfo("Asia/Kolkata")

WINDOW_DAYS = 90
MIN_POSTS = 4  # below this the numbers are anecdotes
MAX_MEDIA = 30  # per sync: ~35 calls, well inside 200 per user per hour
RESYNC_HOURS = 20  # a brand is synced at most once per this many hours by the sweep
# How often a post's numbers are re-read, by its age. Meta says data can lag
# up to 48h, and a post keeps collecting for about a week.
YOUNG_DAYS, YOUNG_EVERY = 7, timedelta(hours=6)
SETTLING_DAYS, SETTLING_EVERY = 30, timedelta(days=3)
OLD_EVERY = timedelta(days=14)
# A follow-up is proposed for a post that beat the account's median rate by
# this much, once it is old enough to have finished collecting.
SEQUEL_LIFT = 1.5
SEQUEL_MIN_AGE_DAYS = 10
SEQUEL_MIN_REACH = 100
SEQUEL_COOLDOWN_DAYS = 30

KIND_NAMES = {"single": "single-image posts", "carousel": "carousels", "reel": "reels"}


def score(row: Any) -> int:
    """Weighted engagement. Saves and shares are the strong signals."""
    return (
        int(getattr(row, "likes", 0) or 0)
        + 2 * int(getattr(row, "comments", 0) or 0)
        + 3 * int(getattr(row, "saved", 0) or 0)
        + 3 * int(getattr(row, "shares", 0) or 0)
        + 5 * int(getattr(row, "follows", 0) or 0)
    )


def rate(row: Any) -> float:
    """Engagement per account reached; the number that compares posts fairly."""
    return score(row) / max(1, int(getattr(row, "reach", 0) or 0))


def kind_of(row: Any) -> str:
    if (getattr(row, "media_product_type", None) or "").upper() == "REELS":
        return "reel"
    if (getattr(row, "media_type", None) or "").upper() == "CAROUSEL_ALBUM":
        return "carousel"
    return "single"


def title_of(row: Any) -> str:
    facts = getattr(row, "facts", None) or {}
    text = facts.get("headline") or (getattr(row, "caption", None) or "").strip().splitlines()
    if isinstance(text, list):
        text = text[0] if text else ""
    text = " ".join(str(text).split())
    return (text[:47] + "...") if len(text) > 50 else (text or "an untitled post")


@dataclass
class Stat:
    n: int = 0
    reach: int = 0
    score: int = 0
    saved: int = 0
    shares: int = 0

    def add(self, row: Any) -> None:
        self.n += 1
        self.reach += int(row.reach or 0)
        self.score += score(row)
        self.saved += int(row.saved or 0)
        self.shares += int(row.shares or 0)

    @property
    def avg_reach(self) -> float:
        return self.reach / self.n if self.n else 0.0

    @property
    def rate(self) -> float:
        return self.score / max(1, self.reach)

    @property
    def avg_saved(self) -> float:
        return self.saved / self.n if self.n else 0.0


@dataclass
class Performance:
    posts: int = 0
    window_days: int = WINDOW_DAYS
    median_rate: float = 0.0
    median_reach: float = 0.0
    by_kind: dict[str, Stat] = field(default_factory=dict)
    by_template: dict[str, Stat] = field(default_factory=dict)
    by_intent: dict[str, Stat] = field(default_factory=dict)
    by_aspect: dict[str, Stat] = field(default_factory=dict)
    by_slot: dict[tuple[int, int], Stat] = field(default_factory=dict)  # (weekday, hour) IST
    top: list[dict[str, Any]] = field(default_factory=list)
    followers: int | None = None
    followers_delta_7d: int | None = None
    synced_at: datetime | None = None

    @property
    def enough(self) -> bool:
        return self.posts >= MIN_POSTS

    @staticmethod
    def best(table: dict, min_n: int = 2) -> tuple[Any, Stat] | None:
        """The key with the best rate among those seen at least min_n times --
        only when there is something to compare it with."""
        eligible = [(k, s) for k, s in table.items() if s.n >= min_n]
        if len(eligible) < 2:
            return None
        eligible.sort(key=lambda ks: (ks[1].rate, ks[1].avg_reach), reverse=True)
        return eligible[0]

    def as_prompt_block(self) -> str:
        if not self.enough:
            return ""
        lines = [
            "## What their followers respond to (Instagram Insights, last "
            f"{self.window_days} days, {self.posts} posts)"
        ]
        single = self.by_kind.get("single")
        reel = self.by_kind.get("reel")
        car = self.by_kind.get("carousel")
        if reel and single and single.n >= 2 and single.avg_reach > 0:
            x = reel.avg_reach / single.avg_reach
            if x >= 1.3:
                lines.append(
                    f"- Reels reach {x:.1f}x their single-image posts so far ({reel.n} reel"
                    f"{'s' if reel.n != 1 else ''} vs {single.n} singles): propose a reel when "
                    "the subject moves -- a process, a before/after, the shop at work."
                )
        if car and single and single.n >= 2 and car.avg_saved > single.avg_saved * 1.5:
            lines.append(
                f"- Carousels are saved most (avg {car.avg_saved:.0f} saves vs "
                f"{single.avg_saved:.0f} on singles): how-tos and lists as carousels."
            )
        best_t = self.best(self.by_template)
        if best_t:
            key, st = best_t
            others = [s for k, s in self.by_template.items() if k != key and s.n >= 2]
            if others:
                rest = max(others, key=lambda s: s.rate)
                lines.append(
                    f"- Layout that performs: {TEMPLATE_NAMES.get(key, key)} "
                    f"({st.rate * 100:.0f} engagements per 100 reached vs "
                    f"{rest.rate * 100:.0f} for the next best)."
                )
        best_i = self.best(self.by_intent)
        if best_i:
            key, st = best_i
            lines.append(
                f"- Post type that performs: {key.replace('_', ' ')} "
                f"({st.rate * 100:.0f} per 100 reached, {st.n} posts)."
            )
        if self.top:
            shown = "; ".join(
                f'"{t["title"]}" ({t["kind"]}, reach {t["reach"]:,}, '
                f"{t['saved']} saves, {t['shares']} shares)"
                for t in self.top[:2]
            )
            lines.append(f"- Their best posts: {shown}.")
        slot = self.best(self.by_slot)
        if slot:
            (wd, hour), st = slot
            lines.append(
                f"- Posts go up best around {_slot_name(wd, hour)} IST so far ({st.n} posts)."
            )
        if len(lines) == 1:
            return ""
        lines.append(
            'Say "so far" -- this is a small sample, not a law. Numbers arrive a day or '
            "two after a post; never promise reach."
        )
        return "\n".join(lines)


_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _slot_name(weekday: int, hour: int) -> str:
    h = hour % 12 or 12
    return f"{_DAYS[weekday]} {h}{'am' if hour < 12 else 'pm'}"


def _hour_bucket(hour: int) -> int:
    """Two-hour buckets: 18 and 19 are one slot, not two samples of one."""
    return (hour // 2) * 2


def rows_for(db: Session, brand_id: uuid.UUID, *, now: datetime | None = None) -> list[PostMetric]:
    now = now or datetime.now(UTC)
    return db.scalars(
        select(PostMetric)
        .where(
            PostMetric.brand_id == brand_id,
            PostMetric.posted_at >= now - timedelta(days=WINDOW_DAYS),
            PostMetric.reach > 0,
        )
        .order_by(PostMetric.posted_at.desc())
        .limit(200)
    ).all()


def build(db: Session, brand_id: uuid.UUID, *, now: datetime | None = None) -> Performance:
    """Counts over post_metrics. Reads only; a few milliseconds."""
    now = now or datetime.now(UTC)
    rows = rows_for(db, brand_id, now=now)
    perf = Performance(posts=len(rows))
    if not rows:
        return perf
    rates = [rate(r) for r in rows]
    perf.median_rate = statistics.median(rates)
    perf.median_reach = statistics.median(int(r.reach or 0) for r in rows)
    perf.synced_at = max((r.synced_at for r in rows if r.synced_at), default=None)
    kinds: dict[str, Stat] = defaultdict(Stat)
    templates: dict[str, Stat] = defaultdict(Stat)
    intents: dict[str, Stat] = defaultdict(Stat)
    aspects: dict[str, Stat] = defaultdict(Stat)
    slots: dict[tuple[int, int], Stat] = defaultdict(Stat)
    for r in rows:
        kinds[kind_of(r)].add(r)
        f = r.facts or {}
        if f.get("template"):
            templates[str(f["template"])].add(r)
        if f.get("intent"):
            intents[str(f["intent"])].add(r)
        if f.get("aspect"):
            aspects[str(f["aspect"])].add(r)
        if r.posted_at:
            local = r.posted_at.astimezone(IST)
            slots[(local.weekday(), _hour_bucket(local.hour))].add(r)
    perf.by_kind, perf.by_template = dict(kinds), dict(templates)
    perf.by_intent, perf.by_aspect, perf.by_slot = dict(intents), dict(aspects), dict(slots)
    ranked = sorted(rows, key=lambda r: (rate(r), int(r.reach or 0)), reverse=True)
    perf.top = [_top_entry(r, perf.median_rate) for r in ranked[:3]]
    stats = db.scalars(
        select(IgAccountStat)
        .where(IgAccountStat.brand_id == brand_id, IgAccountStat.followers.is_not(None))
        .order_by(IgAccountStat.day.desc())
        .limit(30)
    ).all()
    if stats:
        perf.followers = stats[0].followers
        week_ago = stats[0].day - timedelta(days=7)
        older = [s for s in stats if s.day <= week_ago]
        if older and older[0].followers is not None:
            perf.followers_delta_7d = stats[0].followers - older[0].followers
    return perf


def _top_entry(r: PostMetric, median_rate: float) -> dict[str, Any]:
    return {
        "media_id": r.ig_media_id,
        "title": title_of(r),
        "kind": kind_of(r),
        "reach": int(r.reach or 0),
        "likes": int(r.likes or 0),
        "comments": int(r.comments or 0),
        "saved": int(r.saved or 0),
        "shares": int(r.shares or 0),
        "rate": round(rate(r), 4),
        "lift": round(rate(r) / median_rate, 2) if median_rate else None,
        "posted_at": r.posted_at.isoformat() if r.posted_at else None,
        "permalink": r.permalink,
        "facts": dict(r.facts or {}),
        "ours": r.publication_id is not None,
    }


# --------------------------------------------------------------------------- #
# the daily idea
# --------------------------------------------------------------------------- #
def _reel_format_available() -> bool:
    from typing import get_args

    from app.creative.brief import Format

    return "reel" in get_args(Format.model_fields["type"].annotation)


def _guess_intent(entry: dict[str, Any]) -> str:
    facts = entry.get("facts") or {}
    if facts.get("intent"):
        return str(facts["intent"])
    title = (entry.get("title") or "").lower()
    if any(w in title for w in ("% off", "offer", "special", "sale", "discount", "free")):
        return "promo"
    if entry.get("kind") == "carousel" or any(w in title for w in ("how", "ways", "tips")):
        return "educational"
    if any(w in title for w in ("customer", "review", "thank")):
        return "testimonial"
    return "behind_the_scenes"


def sequel_for(
    db: Session,
    brand_id: uuid.UUID,
    perf: Performance,
    *,
    template: str,
    aspect: str,
    play: str,
    today: date | None = None,
) -> dict[str, Any] | None:
    """An Idea dict for a follow-up to the post that beat the account's usual.

    Only when the sample is big enough, the post has finished collecting, it
    beat the median by SEQUEL_LIFT, and no sequel to it was offered in the
    last month.
    """
    if not perf.enough or not perf.top:
        return None
    today = today or datetime.now(IST).date()
    best = None
    for cand in perf.top:
        if not cand.get("posted_at") or cand["reach"] < SEQUEL_MIN_REACH:
            continue
        posted = datetime.fromisoformat(cand["posted_at"]).astimezone(IST).date()
        if (today - posted).days < SEQUEL_MIN_AGE_DAYS:
            continue  # still collecting; judge it next week
        if (cand.get("lift") or 0) < SEQUEL_LIFT:
            break  # ranked by rate: nothing below this one qualifies either
        if _sequel_offered_recently(db, brand_id, cand["media_id"]):
            continue
        best = cand
        break
    if best is None:
        return None
    lift = best["lift"]
    facts = best.get("facts") or {}
    kind = best["kind"]
    fmt = "single"
    slides = 1
    if kind == "carousel":
        fmt, slides = "carousel", 3
    elif kind == "reel" and _reel_format_available():
        fmt = "reel"
    strong = "saves" if best["saved"] >= best["shares"] else "shares"
    n = best["saved"] if strong == "saves" else best["shares"]
    title = best["title"].removesuffix("...")
    return {
        "rank": 0,
        "why": (
            f'your post "{best["title"]}" reached {best["reach"]:,} with {n} {strong}, '
            f"{lift:.1f}x your usual -- a follow-up in the same style"
        ),
        "intent": _guess_intent(best),
        "headline_idea": f"{title}: part 2"[:60],
        "format": fmt,
        "slide_count": slides,
        "template": str(facts.get("template") or template),
        "aspect_ratio": str(facts.get("aspect") or aspect),
        "visual_direction": (
            f"same subject as their best post, a new angle; {facts.get('mood') or 'real, close'}"
            + (f"; {play}" if play else "")
        ),
        "mood": str(facts.get("mood") or "same as what worked"),
        "sequel_of": best["media_id"],
    }


def _sequel_offered_recently(db: Session, brand_id: uuid.UUID, media_id: str) -> bool:
    from app.db.models import CreativeEvent

    rows = db.scalars(
        select(CreativeEvent)
        .where(
            CreativeEvent.brand_id == brand_id,
            CreativeEvent.kind == "suggested",
            CreativeEvent.created_at >= datetime.now(UTC) - timedelta(days=SEQUEL_COOLDOWN_DAYS),
        )
        .order_by(CreativeEvent.created_at.desc())
        .limit(60)
    ).all()
    for r in rows:
        for idea in (r.meta or {}).get("ideas") or []:
            if isinstance(idea, dict) and idea.get("sequel_of") == media_id:
                return True
    return False


# --------------------------------------------------------------------------- #
# the Monday readout
# --------------------------------------------------------------------------- #
_READOUT = {
    "en": (
        "Last week on Instagram: {posts} post{s}, reach {reach:,}{trend}, {saves} saves."
        "{best}{followers}"
    ),
    "hi": (
        "Pichhle hafte Instagram par: {posts} post{s}, reach {reach:,}{trend}, {saves} saves."
        "{best}{followers}"
    ),
}
_TREND = {
    "en": {"up": " (up {pct}% on the week before)", "down": " (down {pct}% on the week before)"},
    "hi": {"up": " (pichhle hafte se {pct}% zyada)", "down": " (pichhle hafte se {pct}% kam)"},
}
_BEST = {"en": ' Best: "{title}" with {n} {what}.', "hi": ' Sabse achha: "{title}", {n} {what}.'}
_FOLLOWERS = {"en": " Followers: {n:,} ({delta}).", "hi": " Followers: {n:,} ({delta})."}


def week_readout(db: Session, brand_id: uuid.UUID, today: date, lang: str = "en") -> str | None:
    """Two lines about last week, or None when there was nothing to report.

    Post reach is summed over the posts that went up last week (Mon-Sun IST)
    and compared with the week before; the follower line appears when two
    daily snapshots a week apart exist.
    """
    lang = lang if lang in _READOUT else "en"
    this_monday = today - timedelta(days=today.weekday())
    last_monday = this_monday - timedelta(days=7)
    prev_monday = last_monday - timedelta(days=7)
    rows = db.scalars(
        select(PostMetric).where(
            PostMetric.brand_id == brand_id,
            PostMetric.posted_at >= _at_midnight(prev_monday),
            PostMetric.posted_at < _at_midnight(this_monday),
        )
    ).all()
    last = [r for r in rows if r.posted_at and r.posted_at >= _at_midnight(last_monday)]
    prev = [r for r in rows if r.posted_at and r.posted_at < _at_midnight(last_monday)]
    if not last:
        return None
    reach = sum(int(r.reach or 0) for r in last)
    saves = sum(int(r.saved or 0) for r in last)
    if reach == 0:
        return None  # numbers not in yet: a readout of zeros would alarm them
    trend = ""
    prev_reach = sum(int(r.reach or 0) for r in prev)
    if prev_reach > 0 and prev:
        pct = round(abs(reach - prev_reach) / prev_reach * 100)
        if pct >= 5:
            trend = _TREND[lang]["up" if reach >= prev_reach else "down"].format(pct=pct)
    best_row = max(last, key=lambda r: (rate(r), int(r.reach or 0)))
    what, n = ("saves", int(best_row.saved or 0))
    if int(best_row.shares or 0) > n:
        what, n = "shares", int(best_row.shares or 0)
    best = _BEST[lang].format(title=title_of(best_row), n=n, what=what) if len(last) > 1 else ""
    followers = ""
    perf_stats = db.scalars(
        select(IgAccountStat)
        .where(IgAccountStat.brand_id == brand_id, IgAccountStat.followers.is_not(None))
        .order_by(IgAccountStat.day.desc())
        .limit(30)
    ).all()
    if perf_stats:
        latest = perf_stats[0]
        older = [s for s in perf_stats if s.day <= latest.day - timedelta(days=6)]
        if older and older[0].followers is not None and latest.followers is not None:
            delta = latest.followers - older[0].followers
            followers = _FOLLOWERS[lang].format(n=latest.followers, delta=f"{delta:+d}")
    return _READOUT[lang].format(
        posts=len(last),
        s="" if len(last) == 1 else "s",
        reach=reach,
        trend=trend,
        saves=saves,
        best=best,
        followers=followers,
    )


def _at_midnight(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=IST).astimezone(UTC)


# --------------------------------------------------------------------------- #
# the sync
# --------------------------------------------------------------------------- #
def _due(media: insights.IgMedia, known: tuple[datetime | None, datetime | None] | None, now):
    """Re-read while young, less often as the numbers settle."""
    if known is None:
        return True
    synced_at, posted_at = known
    if synced_at is None:
        return True
    age = now - (posted_at or media.timestamp or now)
    if age <= timedelta(days=YOUNG_DAYS):
        return now - synced_at >= YOUNG_EVERY
    if age <= timedelta(days=SETTLING_DAYS):
        return now - synced_at >= SETTLING_EVERY
    return now - synced_at >= OLD_EVERY


def synced_recently(brand: Brand, now: datetime | None = None, hours: int = RESYNC_HOURS) -> bool:
    stamp = (brand.template_prefs or {}).get("insights_synced_at")
    if not stamp:
        return False
    try:
        last = datetime.fromisoformat(str(stamp))
    except ValueError:
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    return (now or datetime.now(UTC)) - last < timedelta(hours=hours)


async def sync_brand(brand_id: uuid.UUID, *, now: datetime | None = None) -> dict[str, Any]:
    """Read the account's posts and their numbers into post_metrics.

    Network calls happen outside any transaction. A dead token disconnects
    the account the way a failed publish does, so the owner is asked to
    reconnect once rather than every morning.
    """
    now = now or datetime.now(UTC)
    with session_scope() as db:
        ig_row = db.scalar(
            select(IgAccount)
            .where(IgAccount.brand_id == brand_id, IgAccount.status == "connected")
            .limit(1)
        )
        if ig_row is None:
            return {"ok": False, "reason": "instagram_not_connected"}
        if not insights.can_read_insights(ig_row.scopes):
            return {"ok": False, "reason": "insights_permission_missing"}
        token, ig_user_id, ig_row_id = ig_row.access_token, ig_row.ig_user_id, ig_row.id
        known = {
            r.ig_media_id: (r.synced_at, r.posted_at)
            for r in db.scalars(select(PostMetric).where(PostMetric.brand_id == brand_id))
        }
    since = now - timedelta(days=WINDOW_DAYS)
    yesterday = now.astimezone(IST).date() - timedelta(days=1)
    try:
        media = await insights.list_media(
            access_token=token, ig_user_id=ig_user_id, since=since, limit=MAX_MEDIA
        )
        due = [m for m in media if _due(m, known.get(m.id), now)]
        numbers = await insights.gather_media(access_token=token, media=due)
        profile = None
        try:
            profile = await ig.get_profile(token)
        except Exception as exc:  # noqa: BLE001 - followers are a nicety
            if ig.is_auth_error(exc):
                raise
            log.warning("ig_profile_failed", brand_id=str(brand_id), error=str(exc)[:120])
        account: dict[str, int] = {}
        try:
            account = await insights.account_insights(
                access_token=token, ig_user_id=ig_user_id, since=yesterday, until=yesterday
            )
        except Exception as exc:  # noqa: BLE001 - per-post numbers are the point
            if ig.is_auth_error(exc):
                raise
            log.warning("ig_account_insights_failed", brand_id=str(brand_id), error=str(exc)[:120])
    except Exception as exc:  # noqa: BLE001
        if ig.is_auth_error(exc):
            with session_scope() as db:
                row = db.get(IgAccount, ig_row_id)
                if row is not None:
                    row.status = "disconnected"
            log.warning("ig_insights_token_dead", brand_id=str(brand_id))
            return {"ok": False, "reason": "instagram_reconnect_required"}
        log.exception("ig_insights_sync_failed", brand_id=str(brand_id))
        return {"ok": False, "reason": "sync_failed", "error": str(exc)[:200]}

    ids = [m.id for m in media]
    refreshed = failed = 0
    with session_scope() as db:
        pubs: dict[str, tuple[Publication, Creative]] = {}
        if ids:
            for pub, cr in db.execute(
                select(Publication, Creative)
                .join(Creative, Publication.creative_id == Creative.id)
                .where(Creative.brand_id == brand_id, Publication.ig_media_id.in_(ids))
                .order_by(Publication.published_at.desc())
            ):
                pubs.setdefault(pub.ig_media_id, (pub, cr))
        rows = {
            r.ig_media_id: r
            for r in db.scalars(
                select(PostMetric).where(
                    PostMetric.brand_id == brand_id, PostMetric.ig_media_id.in_(ids or [""])
                )
            )
        }
        for m in media:
            row = rows.get(m.id)
            if row is None:
                row = PostMetric(brand_id=brand_id, ig_media_id=m.id)
                db.add(row)
            row.media_type = (m.media_type or "IMAGE")[:24]
            if m.media_product_type:
                row.media_product_type = m.media_product_type[:24]
            row.permalink = m.permalink or row.permalink
            if m.caption:
                row.caption = m.caption[:2200]
            row.posted_at = m.timestamp or row.posted_at
            got = numbers.get(m.id)
            if isinstance(got, dict):
                for col in (
                    "reach",
                    "views",
                    "likes",
                    "comments",
                    "saved",
                    "shares",
                    "follows",
                    "profile_visits",
                    "total_interactions",
                ):
                    if col in got:
                        setattr(row, col, max(0, int(got[col])))
                row.raw = {**(row.raw or {}), "insights": got}
                row.synced_at = now
                refreshed += 1
            elif got is not None:
                failed += 1
            if row.publication_id is None and m.id in pubs:
                pub, cr = pubs[m.id]
                row.publication_id = pub.id
                brief = db.get(Brief, cr.brief_id)
                if brief is not None:
                    payload = brief.payload or {}
                    row.facts = {
                        **events.facts_of(payload),
                        "headline": (payload.get("headline") or "")[:80],
                        "brief_id": str(brief.id),
                    }
        if profile is not None or account:
            stat = db.scalar(
                select(IgAccountStat).where(
                    IgAccountStat.brand_id == brand_id, IgAccountStat.day == yesterday
                )
            )
            if stat is None:
                stat = IgAccountStat(brand_id=brand_id, day=yesterday)
                db.add(stat)
            if profile is not None:
                stat.followers = profile.followers_count
                stat.media_count = profile.media_count
            if account:
                stat.reach = int(account.get("reach", 0))
                stat.accounts_engaged = int(account.get("accounts_engaged", 0))
                stat.total_interactions = int(account.get("total_interactions", 0))
                stat.raw = account
        brand = db.get(Brand, brand_id)
        if brand is not None:
            brand.template_prefs = {
                **(brand.template_prefs or {}),
                "insights_synced_at": now.isoformat(),
            }
    log.info(
        "ig_insights_synced",
        brand_id=str(brand_id),
        posts=len(media),
        refreshed=refreshed,
        failed=failed,
    )
    return {"ok": True, "posts": len(media), "refreshed": refreshed, "failed": failed}


async def sync_if_due(brand_id: uuid.UUID, *, now: datetime | None = None) -> dict[str, Any] | None:
    """The daily job's entry point: at most one sync per RESYNC_HOURS."""
    now = now or datetime.now(UTC)
    with session_scope() as db:
        brand = db.get(Brand, brand_id)
        if brand is None or synced_recently(brand, now):
            return None
    return await sync_brand(brand_id, now=now)


def sweep(now: datetime | None = None, *, limit: int = 500) -> int:
    """Queue a sync for every connected account not read today. Hourly, on the worker.

    Dedupe is per brand per day, so an hourly sweep syncs each brand once a
    day, and a brand connected at 3pm is read that same hour rather than
    waiting for the morning.
    """
    from app.queue.client import enqueue

    now = now or datetime.now(UTC)
    day = now.astimezone(IST).date().isoformat()
    queued = 0
    with session_scope() as db:
        pairs = db.execute(
            select(IgAccount.brand_id, IgAccount.scopes, Brand)
            .join(Brand, Brand.id == IgAccount.brand_id)
            .where(IgAccount.status == "connected")
            .limit(limit)
        ).all()
        todo = [
            bid
            for bid, scopes, brand in pairs
            if insights.can_read_insights(scopes) and not synced_recently(brand, now)
        ]
    for i, bid in enumerate(todo):
        try:
            if enqueue(
                kind="sync_insights",
                payload={"brand_id": str(bid)},
                dedupe_key=f"insights:{bid}:{day}",
                scheduled_for=now + timedelta(seconds=5 * i),
            ):
                queued += 1
        except Exception:  # noqa: BLE001 - the next sweep tries again
            log.exception("insights_sweep_enqueue_failed", brand_id=str(bid))
    if queued:
        log.info("insights_sweep", queued=queued)
    return queued


def schedule_after_publish(brand_id: uuid.UUID, media_id: str, *, delay: timedelta | None = None):
    """First read of a fresh post, once Meta's numbers have had time to land."""
    from app.queue.client import enqueue

    try:
        enqueue(
            kind="sync_insights",
            payload={"brand_id": str(brand_id)},
            dedupe_key=f"insights:{brand_id}:post:{media_id}",
            scheduled_for=datetime.now(UTC) + (delay or timedelta(hours=48)),
        )
    except Exception:  # noqa: BLE001 - the sweep covers it
        log.warning("insights_after_publish_schedule_failed", brand_id=str(brand_id))


# --------------------------------------------------------------------------- #
# the agent's view
# --------------------------------------------------------------------------- #
def summary(db: Session, brand_id: uuid.UUID, *, now: datetime | None = None) -> dict[str, Any]:
    """Everything the post_performance tool returns."""
    perf = build(db, brand_id, now=now)
    today = (now or datetime.now(UTC)).astimezone(IST).date()

    def table(t: dict, names: dict | None = None) -> dict[str, Any]:
        return {
            (names or {}).get(k, k) if isinstance(k, str) else _slot_name(*k): {
                "posts": s.n,
                "avg_reach": round(s.avg_reach),
                "per_100_reached": round(s.rate * 100, 1),
                "avg_saves": round(s.avg_saved, 1),
            }
            for k, s in sorted(t.items(), key=lambda ks: ks[1].rate, reverse=True)
        }

    best_slot = perf.best(perf.by_slot)
    return {
        "posts": perf.posts,
        "window_days": perf.window_days,
        "enough": perf.enough,
        "followers": perf.followers,
        "followers_delta_7d": perf.followers_delta_7d,
        "median_reach": round(perf.median_reach),
        "by_format": table(perf.by_kind, KIND_NAMES),
        "by_layout": table(perf.by_template, TEMPLATE_NAMES),
        "by_post_type": table(perf.by_intent),
        "best_time": _slot_name(*best_slot[0]) + " IST" if best_slot else None,
        "top": [{k: v for k, v in t.items() if k not in ("facts", "media_id")} for t in perf.top],
        "last_week": week_readout(db, brand_id, today, "en"),
        "synced_at": perf.synced_at.isoformat() if perf.synced_at else None,
    }
