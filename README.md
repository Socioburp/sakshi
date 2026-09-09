# Sakshi v2

SocioBurp's WhatsApp marketing-creative bot for SMBs. A shop owner sends a voice
note; a finished, on-brand social creative comes back.

This is the v2 rebuild: one agentic pipeline instead of v1's intent router, and
Instagram Login instead of the Facebook-Page path.

## The two decisions the codebase is built around

**One vector column, three lanes.** `brand_memory.embedding` is it. Brand identity —
tone, palette, audience, and the `never_say` list — lives in plain columns on
`brands` and goes into every prompt whole. Nothing about the brand is retrieved,
so a `never_say` rule can never be missed because it ranked below a similarity
threshold. Retrieval is for the long tail only: past creatives, feedback,
product notes.

**The brief forbids text in the image prompt.** `visual_direction.prompt`
generates the background/hero image only; headline, subhead, CTA and logo are
composited afterwards in the brand's real fonts. The validator enforces it at
the type boundary — a prompt containing "text", "logo", "watermark", "price tag"
or a quoted string is rejected before it reaches the image model.

The brief itself is defined by `docs/brief_schema.json`; `app/creative/brief.py`
is the pydantic implementation of it, and a test asserts the two cannot drift.

That split is the cost model. A copy change is `revise_creative`: reuse the
stored background, re-composite (~700ms locally), charge nothing. Only
`regenerate_image` pays the image model.

## Layout

```
app/
  main.py                  FastAPI: /health, /health/ready, webhooks, OAuth callback
  config.py                pydantic-settings; the only reader of os.environ
  db/                      models.py, session.py (pooled), repo.py
  channels/
    base.py                provider-agnostic Inbound/Outbound contract
    whatsapp/
      router.py            verification handshake + inbound fan-in
      ingest.py            persist -> extend window -> enqueue (nothing else)
      send.py              every outbound send, window-checked, mirrored to messages
      session_window.py    Meta's 24h rule
      adapters/            meta.py | gupshup.py | twilio.py | mock.py
  agent/
    runner.py              the loop
    tools.py               9 tools; brief schema generated from the pydantic model
    prompts.py             system prompt; brand identity injected whole
    language.py            which language AND which script to answer in
    context.py             what a tool is allowed to touch
  creative/
    brief.py               the contract + the no-text-in-prompt validator
    logo.py                palette measured from pixels; character read by vision
    pipeline.py            generate | recompose | regenerate_image; slides in parallel
    imagegen/              provider interface + two candidates + mock
    compose.py             headless Chromium, one long-lived browser
  memory/
    embed.py               Voyage embeddings, brand_memory only
    retrieve.py            cosine search, optional precomputed vector
    grounding.py           the three lanes the brief contract names
  integrations/
    instagram/             STUBBED behind INSTAGRAM_MOCK; four real signatures
    stt/                   elevenlabs | deepgram | sarvam | mock
    storage/r2.py          public URLs, drafts/ vs published/, lifecycle rule
    razorpay/              payment links + webhook -> credits
  billing/credits.py       ledger + balance under one row lock
  queue/                   client.py, worker.py, handlers.py
  telemetry/stages.py      per-stage latency -> stage_timings
migrations/                0001_initial.py (raw DDL, pgvector, no HNSW yet)
templates/creative/        centered_overlay | lower_third | split_card
docs/brief_schema.json     the canonical brief contract
scripts/
  stt_bakeoff.py           scores product-name recall, not WER
  milestone1_smoke.py      the whole path, every vendor stubbed
```

## Run it

```bash
make install                 # deps + chromium
cp .env.example .env         # fill in per PROVISIONING.md
alembic upgrade head
make dev                     # api
make worker                  # in another shell
```

With `WA_PROVIDER=mock`, `STT_PROVIDER=mock` and `IMAGEGEN_PROVIDER=mock` the
whole product runs with no vendor accounts at all:

```bash
make smoke     # the full client journey; writes out/journey_*.png
make test
```

## Build order — where this stands

| # | Step | Status |
|---|---|---|
| 1 | Provision Neon | done — project `sakshi`, ap-southeast-1, PG 18.6, extensions + schema applied |
| 1b | Provision R2 / Upstash / API keys | **yours** — see `PROVISIONING.md`; these mint secrets, so they aren't mine to create |
| 1c | GitHub repo | done — `Socioburp/sakshi` (private); initial commit made locally, `git push -u origin main` is yours |
| 2 | Run the migration against Neon | done — schema live in Singapore; `0002` adds carousel + brand assets, apply with `alembic upgrade head` |
| 3 | FastAPI skeleton, `/health`, webhook handshake | done |
| 4 | Instagram stubbed with four real signatures | done — flip `INSTAGRAM_MOCK` when Track A lands |
| 5 | Ingestion: webhook → `messages` → `wa_sessions`, text and audio split | done |
| 6 | STT bake-off | harness done — **needs your 20–30 real voice notes** |

**Milestone 1 is met in code**: a voice note arrives, is transcribed, and comes
back as a validated brief and a rendered creative in the WhatsApp reply.
`make smoke` proves the path end to end with the real app, real ingestion, real
agent loop, real validator, real templates and real Chromium — only Anthropic,
STT, the image model and R2 are stubbed.

It is not proven against *reality* until step 6: record the voice notes, run
`make bakeoff`, and pick the STT provider on product-name recall. That is the
decision most likely to be wrong if you choose on vendor benchmarks.

## What is deliberately left blank

- `ANTHROPIC_MODEL` — copy the exact id from the console; the app refuses to
  start the agent without it rather than guessing.
- `ProviderA.ENDPOINT` / `ProviderB.ENDPOINT` in `creative/imagegen/providers.py`
  — fill in for your two candidates. A wrong payload shape here fails silently
  as a blank background, so it is left explicit rather than guessed.
- The HNSW index on `brand_memory.embedding`. On an empty table a sequential
  scan is faster. The exact statement is in `migrations/versions/0001_initial.py`;
  run it once you have a few thousand rows.

## How Sakshi talks

`app/agent/language.py` decides two things on every message, and they are always
decided together: the **language**, and the **script to write it in**.

That second half is the one that matters and the one usually got wrong. A shop
owner who types `kal se sale hai` is writing Hindi in Latin letters because that
is what their keyboard does. Replying in Devanagari is not more respectful — it
is unreadable on their phone, and it reads as a machine that did not notice.

So detection returns both, and the system prompt is given an explicit rule plus a
worked example in that exact language, rather than the old one-line hope that the
model would "mirror the client".

Detection is deliberately conservative. Native script wins outright. Romanised
Indic needs at least two distinctive marker words and a real share of the message,
so a stray "kal" in an English sentence cannot flip a London client into Hinglish.
Below that bar it answers in English — stiff English costs a little, confident
Marathi to someone who does not speak it costs the client.

The locale it locks is reused as the speech-to-text hint. A Kannada voice note
transcribed as Hindi loses precisely the words that matter: the product names.

## What the client actually experiences

1. They message the WhatsApp number. The bot asks **one** question: what business
   they run. Not a form, not a feature list.
2. It asks for their logo.
3. They send it. The palette is **measured from the pixels** with Pillow — not
   asked for, not guessed by a vision model. A client who says "our green" and a
   logo that is `#175B3D` are two different facts, and the second is the one every
   creative has to match. Vision is used only for what it is actually good at:
   whether the mark is a wordmark or an emblem, geometric or hand-drawn, premium
   or friendly. Both land in the brand brain.
4. They describe what they want, in a line or two, typed or spoken.
5. The creative arrives on their phone.
6. The bot asks permission to post, with tappable buttons.
7. **Only their tap publishes it.** `creatives.approved_at` is written at ingest
   from the button payload, and `publish_to_instagram` reads that column. A typed
   "yes", or an agent that convinces itself they agreed, does not reach the API —
   the gate is a database column, not a judgement call.

`app/agent/prompts.py::missing_setup` drives step 1–2: the system prompt is told
which single fact is still missing, so onboarding stays inside the one agentic
loop instead of becoming a separate state machine.

## Grounding — how retrieval actually runs

`docs/brief_schema.json` has always defined a `grounding` object with
`catalog_item_ids`, `style_anchor_ids` and `rejection_ids`. `app/memory/grounding.py`
is what fills them.

Three lanes, not one search, because they are different questions and must not
share a threshold:

| Lane | Kinds | Bar | Why |
|---|---|---|---|
| catalogue | `product` | 0.45 | Naming a product they don't stock is worse than naming none |
| style | `style_anchor`, `past_creative`, `feedback` | 0.38 | A near-miss is still useful direction |
| rejections | `rejection` | 0.25 | Missing a "never do this" costs trust; a spurious one costs tokens |

The rejection bar is deliberately the loosest. The cost is asymmetric, so recall
beats precision there.

The question is embedded **once** and reused across all three lanes — three Voyage
calls per turn would be three times the cost and latency for the same vector.

Each lane renders under its own heading in the prompt. One merged list would let a
rejection read as a suggestion.

`brief.grounding` is stamped **server-side** from what retrieval returned, overwriting
whatever the model put there. The field exists to debug retrieval quality, and a
model-authored version records what it believed rather than what it was handed —
exactly backwards when a creative comes out wrong.

## Carousels

**2 to 6 slides** — that is what the product offers today. Instagram allows 10 and
the original schema said 10; the limit lives in `CAROUSEL_MIN`/`CAROUSEL_MAX` in
`brief.py` so it is enforced by the type, not by asking the model nicely. A test
asserts the code and `docs/brief_schema.json` agree.

`format.type: "carousel"` with a `slides[]` array. One creative row per slide,
all sharing a `carousel_group_id`, generated **in parallel** — three slides take
about as long as one, where serial generation would take most of a minute and
read as the bot having died. Publishing follows Instagram's two-level container
dance: a child container per slide, then a parent `CAROUSEL` container. The child
ids are persisted before the parent call, because a crash in between leaves
orphaned containers you cannot identify.

Billing is per image call, so a three-slide carousel costs three credits, and a
partial failure refunds only the slides that did not ship.

## Nothing ships cropped

After the page renders, the compositor measures the real headline box in the
browser and shrinks the type until it fits. If it still overflows at minimum
size, `compose` raises rather than screenshotting — a knowingly-cropped creative
is worse than a failure, because the client sends it to their audience.

The logo is inlined as a data URI before rendering, once per request. A remote
`<img>` would make every creative depend on a fetch finishing inside the render,
and a missing image is not an error — it is a silent hole where the brand mark
should be.

## Real product photos

A slide whose `visual_direction.reference_asset_id` points at a `brand_assets`
row composites over the owner's actual photograph instead of a generated
stand-in. No model call, no charge. `list_brand_assets` puts those ids in front
of the agent.

## Next

Milestone 2 — approval buttons and the first real publish once Instagram app
review clears. After that: an intent-driven template picker, and wiring
`brand_assets` to the WhatsApp image-upload path so product photos accumulate on
their own.
