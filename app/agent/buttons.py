"""Tappable buttons, in the owner's language.

WhatsApp allows three reply buttons of 20 characters. These are the ones the
product uses beyond approval, with titles per locale so a Tamil owner is not
asked to tap "Banao". Ids are stable; ingest passes the tapped id through as
`interactive_id` and the title as the message text.
"""

from __future__ import annotations

from app.channels.base import Button

_TITLES: dict[str, dict[str, str]] = {
    "make:1": {
        "hi": "Banao",
        "en": "Make it",
        "kn": "Maadi",
        "ta": "Pannunga",
        "te": "Cheyandi",
        "mr": "Banva",
        "ml": "Cheyyu",
    },
    "next": {
        "hi": "Doosra idea",
        "en": "Another idea",
        "kn": "Bere idea",
        "ta": "Vera idea",
        "te": "Inko idea",
        "mr": "Dusra idea",
        "ml": "Vere idea",
    },
    "skip": {
        "hi": "Aaj nahi",
        "en": "Not today",
        "kn": "Ivattu beda",
        "ta": "Innaikku venam",
        "te": "Ee roju vaddu",
        "mr": "Aaj nako",
        "ml": "Innu venda",
    },
    "grid:adjusted": {
        "hi": "Grid se match karo",
        "en": "Match my grid",
        "kn": "Grid ge match maadi",
        "ta": "Grid-ku match pannu",
        "te": "Grid ki match cheyi",
        "mr": "Grid shi match kara",
        "ml": "Grid-inu match cheyyu",
    },
    "grid:original": {
        "hi": "Jaisa bola waisa",
        "en": "As I said",
        "kn": "Helidange maadi",
        "ta": "Sonna maari",
        "te": "Cheppinatte",
        "mr": "Mhatla tasa",
        "ml": "Paranja pole",
    },
    "ig:send": {
        "hi": "Bhejo",
        "en": "Send",
        "kn": "Kalisi",
        "ta": "Anuppu",
        "te": "Pampu",
        "mr": "Pathva",
        "ml": "Ayakku",
    },
    "ig:edit": {
        "hi": "Badlo",
        "en": "Edit",
        "kn": "Badalisi",
        "ta": "Maattu",
        "te": "Marchu",
        "mr": "Badla",
        "ml": "Maattu",
    },
    "ig:skip": {
        "hi": "Rehne do",
        "en": "Skip",
        "kn": "Bidi",
        "ta": "Vidu",
        "te": "Vadileyi",
        "mr": "Nako",
        "ml": "Venda",
    },
}

# The reply-loop buttons carry the ig_event id in the tap id, so ingest can
# post exactly the reply the owner approved. Prefixes: igok/iged/igno.
_IG_ACTION_IDS = {"ig:send": "igok", "ig:edit": "iged", "ig:skip": "igno"}


def ig_buttons(event_id: str, locale: str | None) -> list[Button]:
    lang = ((locale or "en").split("-")[0]).lower()
    out = []
    for key, prefix in _IG_ACTION_IDS.items():
        titles = _TITLES[key]
        out.append(Button(id=f"{prefix}:{event_id}", title=titles.get(lang, titles["en"])[:20]))
    return out


def button(button_id: str, locale: str | None) -> Button:
    lang = ((locale or "en").split("-")[0]).lower()
    titles = _TITLES[button_id]
    return Button(id=button_id, title=titles.get(lang, titles["en"])[:20])


def buttons(ids: list[str], locale: str | None) -> list[Button]:
    return [button(i, locale) for i in ids]


def as_payload(ids: list[str], locale: str | None) -> list[dict[str, str]]:
    """The shape a tool result carries; the runner turns it back into Buttons."""
    return [{"id": b.id, "title": b.title} for b in buttons(ids, locale)]
