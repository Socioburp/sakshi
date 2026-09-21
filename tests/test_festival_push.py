"""The festival offer: about THEIR shop, on the right day, to the right owner."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pytest

from app.channels.base import Button, OutboundMessage
from app.channels.whatsapp.adapters.meta import MetaAdapter
from app.config import Settings
from app.db import repo
from app.insights import festival_push as fp
from app.insights import suggest as sg

CAL = [
    {"name": "Diwali", "date": "2026-11-08", "angle": "lights", "verified_on": "2026-09-21"},
    {"name": "Durga Puja", "date": "2026-10-18", "angle": "pandal", "verify": True},
    {"name": "Dussehra", "date": "2026-10-20", "angle": "victory", "verified_on": "2026-09-21"},
    {"name": "Pongal", "date": "2027-01-14", "angle": "harvest", "languages": ["ta"]},
    {"name": "Chhath Puja", "date": "2026-11-15", "angle": "thekua", "languages": ["hi"]},
]


@pytest.fixture
def calendar(monkeypatch):
    monkeypatch.setattr(sg, "load_festivals", lambda path=None: CAL)


# --------------------------------------------------------------------------- #
# which festival, for whom, when
# --------------------------------------------------------------------------- #
def test_a_festival_is_offered_one_to_three_days_out_never_on_the_day(calendar):
    assert fp.due_festival(date(2026, 11, 5), "en-IN").days_away == 3
    assert fp.due_festival(date(2026, 11, 7), "en-IN").days_away == 1
    assert fp.due_festival(date(2026, 11, 4), "en-IN") is None, "four days out: too early"
    assert fp.due_festival(date(2026, 11, 8), "en-IN") is None, "on the day only greets"


def test_an_unverified_date_is_never_pushed(calendar):
    """A wrong 'tomorrow is Durga Puja' is worse than silence."""
    due = fp.due_festival(date(2026, 10, 17), "en-IN")
    assert due is not None and due.name == "Dussehra", "skips the unverified one, finds the next"
    assert fp.due_festival(date(2026, 10, 16), "en-IN") is None


def test_a_regional_festival_goes_only_to_owners_who_keep_it(calendar):
    assert fp.due_festival(date(2027, 1, 12), "ta-IN").name == "Pongal"
    assert fp.due_festival(date(2027, 1, 12), "hi-IN") is None
    assert fp.due_festival(date(2026, 11, 13), "hi").name == "Chhath Puja"
    assert fp.due_festival(date(2026, 11, 13), "kn-IN") is None


def test_the_shipped_calendar_has_diwali_season_verified_and_the_rules_documented():
    data = json.loads(sg.FESTIVALS.read_text(encoding="utf-8"))
    by_name = {f["name"]: f for f in data["festivals"] if f["date"].startswith("2026")}
    for name in ("Navratri", "Dussehra", "Dhanteras", "Diwali", "Bhai Dooj"):
        assert not by_name[name].get("verify") and by_name[name]["verified_on"], name
    assert by_name["Durga Puja"].get("verify") is True, "ambiguous day: left for a human"
    assert "verified_on" in data["note"] and "languages" in data["note"]
    assert fp.due_festival(date(2026, 11, 6), "en-IN").name == "Diwali"


# --------------------------------------------------------------------------- #
# what it says: only facts
# --------------------------------------------------------------------------- #
DUE = fp.Due("Dussehra", date(2026, 10, 20), 3, "victory")


def test_the_line_names_their_photo_and_their_style_only_when_they_exist():
    full = fp.line_for(DUE, {"photo_label": "motichoor box", "usual_style": True}, "en")
    assert full == (
        "Dussehra is in 3 days. Shall I make your post with your motichoor box photo, "
        "in your usual style?"
    )
    bare = fp.line_for(DUE, {"photo_label": None, "usual_style": False}, "en")
    assert bare == "Dussehra is in 3 days. Shall I make your post?"
    assert "photo" not in bare and "style" not in bare


def test_tomorrow_reads_as_tomorrow_and_hindi_is_hindi():
    soon = fp.Due("Diwali", date(2026, 11, 8), 1, "lights")
    assert fp.line_for(soon, {}, "en").startswith("Diwali is tomorrow.")
    hi = fp.line_for(soon, {"photo_label": "kaju katli"}, "hi")
    assert hi == "Diwali kal hai. Aapka post bana doon aapki kaju katli wali photo se?"
    assert fp.line_for(soon, {}, "kn").startswith("Diwali is tomorrow."), "no guessed Kannada"


def test_no_price_promise_is_made_in_a_line_the_agent_does_not_control():
    line = fp.line_for(DUE, {"photo_label": "laddoo", "usual_style": True, "free": True}, "en")
    assert "credit" not in line.lower() and "free" not in line.lower()


def test_template_parameters_are_festival_days_and_what_it_is_built_on():
    assert fp.template_params(DUE, {"photo_label": "motichoor box"}, "Sri Sweets") == [
        "Dussehra",
        "3",
        "your motichoor box photo",
    ]
    assert fp.template_params(DUE, {"photo_label": None}, "Sri Sweets")[2] == "Sri Sweets"


# --------------------------------------------------------------------------- #
# the wire, and the money
# --------------------------------------------------------------------------- #
def test_the_meta_template_payload_carries_the_body_and_the_tappable_payloads():
    msg = OutboundMessage(
        to="919812345678",
        kind="template",
        template_name="festival_offer",
        template_lang="hi",
        template_params=["Diwali", "3", "your kaju katli photo"],
        buttons=[Button(id="make:1", title=""), Button(id="skip", title="")],
    )
    body = MetaAdapter()._build_payload(msg)
    assert body["type"] == "template" and body["to"] == "919812345678"
    t = body["template"]
    assert t["name"] == "festival_offer" and t["language"] == {"code": "hi"}
    assert [p["text"] for p in t["components"][0]["parameters"]] == msg.template_params
    taps = [c for c in t["components"] if c["type"] == "button"]
    assert [(c["index"], c["sub_type"], c["parameters"][0]["payload"]) for c in taps] == [
        ("0", "quick_reply", "make:1"),
        ("1", "quick_reply", "skip"),
    ]


def test_paid_pushes_are_off_by_default_and_capped_when_on():
    assert Settings.model_fields["wa_template_festival"].default == ""
    assert 1 <= Settings.model_fields["festival_push_monthly_cap"].default <= 8
    assert fp.template_configured() is False


def test_the_sweep_only_runs_in_shop_hours(monkeypatch, calendar):
    called = []
    monkeypatch.setattr(sg, "upcoming_festivals", lambda *a, **k: called.append(1) or [])
    night = datetime(2026, 11, 5, 20, 30, tzinfo=UTC)  # 02:00 IST
    assert fp.sweep(night) == 0 and called == [], "did not even look"
    morning = datetime(2026, 11, 5, 4, 30, tzinfo=UTC)  # 10:00 IST
    assert fp.sweep(morning) == 0 and called == [1], "looked, found nothing due, touched no DB"


def test_nudges_off_or_a_blocked_account_means_no_offer(monkeypatch):
    import types

    from app.insights import votes

    monkeypatch.setattr(votes, "nudge_snoozed", lambda brand: False)
    acct = types.SimpleNamespace(blocked_at=None)
    ok = types.SimpleNamespace(template_prefs={})
    assert fp.eligible(ok, acct)
    assert not fp.eligible(types.SimpleNamespace(template_prefs={"daily_nudge": False}), acct)
    assert not fp.eligible(types.SimpleNamespace(template_prefs={"festival_push": False}), acct)
    assert not fp.eligible(ok, types.SimpleNamespace(blocked_at=datetime.now(UTC)))
    assert not fp.eligible(ok, None)
    monkeypatch.setattr(votes, "nudge_snoozed", lambda brand: True)
    assert not fp.eligible(ok, acct)


# --------------------------------------------------------------------------- #
# the tap has to find its idea
# --------------------------------------------------------------------------- #
def test_a_pushed_idea_survives_into_the_session_its_tap_opens():
    now = datetime.now(UTC)
    ideas = [{"rank": 1, "headline_idea": "Diwali special", "reference_asset_id": "abc"}]
    fresh = {"suggestions": ideas, "suggestions_at": (now - timedelta(hours=30)).isoformat()}
    assert repo._carry_over(fresh, now) == fresh

    stale = {"suggestions": ideas, "suggestions_at": (now - timedelta(hours=80)).isoformat()}
    assert repo._carry_over(stale, now) == {}, "a week-old offer is not what the tap means"
    assert repo._carry_over({"suggestions": ideas}, now) == {}, "an in-window nudge is not carried"
    assert repo._carry_over({"suggestions_at": "garbage", "suggestions": ideas}, now) == {}
    assert repo._carry_over({"grid_choice": {"x": 1}}, now) == {}, "nothing else leaks across"
