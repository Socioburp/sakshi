# WhatsApp message templates

Outside the 24-hour customer-service window Meta delivers only **pre-approved
template messages**, and bills each as a marketing conversation (about ₹1 in
India, plus GST — check the rate card in your WhatsApp Business Account; it
changes). Sakshi uses exactly one, for the festival offer
(`app/insights/festival_push.py`). Everything else it sends is inside the
window and free.

Nothing is sent outside the window until `WA_TEMPLATE_FESTIVAL` names a
template that has been approved. In-window festival offers work without it.

## `festival_offer` (category: Marketing)

Create it in **WhatsApp Manager → Message templates → Create template**, once
per language you serve. The code sends `en` to everyone except Hindi-locale
owners, who get `hi`; add more languages there when you have approved bodies
for them.

**Body — English (`en`)**

    {{1}} is in {{2}} days. I can make your post for it using {{3}}. Shall I?

**Body — Hindi (`hi`)**

    {{1}} {{2}} din mein hai. {{3}} se aapka post bana doon?

Sample values for review: `Diwali`, `3`, `your kaju katli box photo`.

| Parameter | Filled with |
|---|---|
| `{{1}}` | festival name, from `docs/festivals_in.json` |
| `{{2}}` | days away (1–3) |
| `{{3}}` | `your <label> photo` when the brand has a product photo on file, otherwise the brand's name |

**Buttons — Quick reply, in this order**

| # | Button text (en / hi) | Payload sent by the code |
|---|---|---|
| 0 | Make it / Banao | `make:1` |
| 1 | Not today / Aaj nahi | `skip` |

The payloads are what matters: the owner's tap arrives as `make:1`, which is
the same id the in-window buttons use, so the agent builds idea #1 — the
festival post, on the owner's own photograph when there is one.

Then set, on the API **and** the worker service:

    WA_TEMPLATE_FESTIVAL=festival_offer
    FESTIVAL_PUSH_MONTHLY_CAP=4        # paid pushes per brand per month

## What keeps this from becoming spam, or a bill

- one offer per brand per festival, ever (`already_pushed`);
- at most `FESTIVAL_PUSH_MONTHLY_CAP` **paid** pushes per brand per month;
- never to an owner who turned nudges off (`daily_nudge` / `festival_push`
  false in `template_prefs`) or snoozed them;
- only between 09:00 and 13:00 IST;
- only for festivals whose date is confirmed — entries still marked
  `"verify": true` in `docs/festivals_in.json` are skipped;
- regional festivals (`"languages": [...]`) only to owners writing in one of
  those languages.

Cost ceiling: `brands × FESTIVAL_PUSH_MONTHLY_CAP × ~₹1` a month. With the
default cap, 1,000 brands is at most about ₹4,000 a month, and only for owners
who were outside the window — the ones who would otherwise have heard nothing.
