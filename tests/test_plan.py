"""The month's plan: pillars, cadence, festival arcs, the daily slot."""

from __future__ import annotations

from datetime import date

from app.insights import plan as P


def test_weighted_pillars_keep_the_mix_and_never_repeat():
    mix = P.MIX_BY_GOAL["footfall"]
    out = P._weighted_pillars(mix, 17)
    assert len(out) == 17
    assert out.count("offer") >= 5  # 0.35 * 17 = 5.95
    assert all(a != b for a, b in zip(out, out[1:], strict=False))
    assert P._weighted_pillars(mix, 0) == []


def test_posting_days_follow_the_cadence():
    days = P._posting_days(date(2026, 10, 1), 4)
    assert all(d.weekday() in (1, 3, 5, 6) for d in days)
    assert 17 <= len(days) <= 18
    assert len(P._posting_days(date(2026, 10, 1), 1)) == 5  # Saturdays in Oct 2026


def test_festival_arc_days_are_before_the_festival():
    # Diwali 2026 falls on 8 Nov (docs/festivals_in.json); the arc lands 5, 2, 1
    # days before, on any weekday: the arc outranks the cadence.
    fest = [f for f in P._festivals_in(date(2026, 11, 1)) if f["name"] == "Diwali"]
    assert fest, "the calendar lost Diwali"
    d = fest[0]["_date"]
    assert d == date(2026, 11, 8)
    for lead, *_ in P.FESTIVAL_ARC:
        assert (d - P.timedelta(days=lead)).month == 11


def test_slot_for_prefers_today_then_a_missed_slot_this_week():
    class Plan:
        slots = [
            {"date": "2026-10-06", "status": "planned", "headline_idea": "a"},  # Tue
            {"date": "2026-10-08", "status": "planned", "headline_idea": "b"},  # Thu
            {"date": "2026-10-10", "status": "made", "headline_idea": "c"},  # Sat
        ]

    assert P.slot_for(Plan(), date(2026, 10, 8))["headline_idea"] == "b"
    # Friday: nothing planned today; the most recent missed slot is offered.
    assert P.slot_for(Plan(), date(2026, 10, 9))["headline_idea"] == "b"
    Plan.slots[1]["status"] = "made"
    assert P.slot_for(Plan(), date(2026, 10, 9))["headline_idea"] == "a"
    # Next Monday: last week's misses are gone.
    assert P.slot_for(Plan(), date(2026, 10, 12)) is None
    assert P.slot_for(None, date(2026, 10, 8)) is None


def test_week_message_lists_the_week_with_ticks():
    class Plan:
        slots = [
            {"date": "2026-10-06", "status": "made", "headline_idea": "Mango pickle is back"},
            {"date": "2026-10-08", "status": "planned", "headline_idea": "3 ways to use ghee"},
            {"date": "2026-10-20", "status": "planned", "headline_idea": "next week"},
        ]

    msg = P.week_message(Plan(), date(2026, 10, 5), "en")
    assert msg.startswith("This week's plan:")
    assert "Tue 06: ✓ Mango pickle is back" in msg and "Thu 08: 3 ways to use ghee" in msg
    assert "next week" not in msg
    assert "Is hafte" in P.week_message(Plan(), date(2026, 10, 5), "hi")
    assert P.week_message(None, date(2026, 10, 5)) is None
