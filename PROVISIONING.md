# Step 1 — Provision the services

I can't create accounts or handle your credentials, so this is the part you run.
Everything below is free-tier to start and takes about twenty minutes end to end.
Each item ends with the `.env` keys it fills.

Region is settled: **Singapore (`ap-southeast-1`)**. Keep everything there.
Every agent turn makes several Postgres round trips, and a cross-region hop adds
roughly 150-200ms to each one, which the shop owner feels as a bot that thinks
slowly. The original project was in Ohio; it was empty, so it was moved.

---

## 1. Neon — DONE

Provisioned on 8 Sep 2026, in the console.

| | |
|---|---|
| Project | `sakshi` (`blue-wave-81695040`), org SocioBurp |
| Region | **AWS Asia Pacific 1 (Singapore)** — `ap-southeast-1` |
| Postgres | 18.6 |
| Branch | `production` (`br-plain-queen-b338e5wq`) |
| Database | `sakshi` (owner `neondb_owner`; the default `neondb` is left empty) |
| Extensions | `vector` 0.8.6, `pgcrypto` 1.4 |
| Schema | 13 tables, 15 foreign keys, 10 check constraints, `brand_memory.embedding` = `vector(1024)`, partial unique index `uq_wa_sessions_live` |
| Alembic | stamped at `0001`, so `alembic upgrade head` is a no-op rather than a re-run |

The schema fingerprint matches a local `alembic upgrade head` against Postgres 16
+ pgvector exactly, so the console-applied schema and the migration file are the
same thing.

**Two things left for you here:**

1. **The connection string.** I deliberately did not copy it — it contains the
   database password, and a secret that passes through a chat transcript should
   be considered burned. Get it yourself from *Connect* → **Pooled connection**
   (the host contains `-pooler`; the direct string will exhaust connections the
   first time two workers run at once), select database `sakshi`, and change the
   prefix `postgresql://` → `postgresql+psycopg://` for SQLAlchemy. Paste it
   straight into Render's env group rather than into a local file.

2. **The old project.** `SB bot` (`solitary-art-03237957`) is still there, in
   Ohio, empty. I left it alone — deleting a project is not something to do on
   inference. Delete it when you're happy this one works; on the Free plan it
   counts against your project allowance.

Set your Render region to **Singapore** to match. That was the point of the move.

## 2. Cloudflare R2

1. R2 → Create bucket, e.g. `sakshi-creatives`, location hint closest to your region.
2. Settings → **Public access** → enable the `r2.dev` dev subdomain (or attach a
   custom domain, which you'll want before launch — `r2.dev` is rate-limited).

   This is not optional and not a convenience. Instagram's container endpoint
   **fetches** `image_url` as an anonymous client on the open internet. A
   signed-URL-only bucket fails at publish time, after the owner has approved
   the post, which is the worst possible place to discover it.
3. Manage R2 API Tokens → create a token with **Object Read & Write** scoped to
   this bucket. Copy the access key id and secret once — the secret is shown once.
4. Lifecycle rule, expire unapproved drafts after 7 days. Either set it in the
   dashboard on prefix `drafts/`, or run:

   ```bash
   python -m app.integrations.storage.r2
   ```

   The code writes drafts under `drafts/` and copies approved creatives to
   `published/`, which has no expiry.

→ `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET`, `R2_PUBLIC_BASE_URL`

## 3. Upstash Redis

1. Create a database, region matching the rest, eviction **off** (this is a queue,
   not a cache — evicting a job loses a paid creative).
2. Copy the `rediss://` URL from the **Redis connect** tab, not the REST URL.

Serverless pricing fits the load shape here: creative generation is bursty and
an always-on Render Redis instance is idle most of the day.

→ `REDIS_URL`

## 4. API keys

| Service | Where | Key |
|---|---|---|
| Anthropic | console.anthropic.com → API keys | `ANTHROPIC_API_KEY` |
| Anthropic | same console → Models; copy the **exact model id** | `ANTHROPIC_MODEL` |
| Voyage | dash.voyageai.com | `VOYAGE_API_KEY` |
| Image provider A | your candidate | `IMAGEGEN_A_API_KEY` |
| Image provider B | your candidate | `IMAGEGEN_B_API_KEY` |
| STT ×3 | ElevenLabs, Deepgram, Sarvam — all three, for the bake-off | `ELEVENLABS_API_KEY`, `DEEPGRAM_API_KEY`, `SARVAM_API_KEY` |

`ANTHROPIC_MODEL` is deliberately left blank in `.env.example` and the app
refuses to start the agent without it. Copy the id from the console rather than
typing one from memory; a wrong id fails at the first model call, inside a
worker, where you'll read it as "the bot went quiet".

## 5. GitHub — DONE

This step was missing from the original plan, and it is the hinge: Render's
blueprint deploys *from a repository*, so "deploy early and get a real HTTPS URL"
is impossible until the code is on GitHub.

Repo created 8 Sep 2026: **`Socioburp/sakshi`**, private, no README/.gitignore/
license so the first push lands clean.

The code I delivered already contains a git repo with the initial commit made and
`origin` set. Unzip it and:

```bash
cd sakshi
git push -u origin main
```

I could not push it for you: this session has no GitHub CLI and no GitHub
connector, and pushing needs a token, which is not something to hand through a
chat. Creating the empty repo is the part that needs no secret; the push is the
part that does.

## 6. Render

1. New Blueprint from the repo — `render.yaml` defines the web service and the
   worker. Set the region to match Neon.
2. Fill the `sakshi-secrets` environment group with everything above.
3. Deploy. Note the HTTPS URL, set it as `PUBLIC_BASE_URL`, and redeploy.

You need that URL before you can register the WhatsApp webhook *or* the
Instagram OAuth redirect, which is why it's worth getting a bare service live
early even though it does nothing yet.

## 7. WhatsApp

Set `WA_PROVIDER` to `meta`, `gupshup` or `twilio`. The code is provider-agnostic;
swapping later is one env var and no code change.

- Callback URL: `https://<your-render-url>/webhooks/whatsapp`
- Verify token: whatever you put in `WA_VERIFY_TOKEN` (make it long and random)
- Subscribe to the `messages` field
- Meta: also set `WA_APP_SECRET` so `X-Hub-Signature-256` is verified. Without it
  the app refuses unsigned webhooks in prod and accepts them in dev.

Check it before you touch the provider dashboard:

```bash
curl "https://<your-render-url>/webhooks/whatsapp?hub.mode=subscribe\
&hub.verify_token=<WA_VERIFY_TOKEN>&hub.challenge=ping"
# -> ping
```

`GET /webhooks/whatsapp/debug/verify-url` prints the exact URL, token and a
self-test link (dev only).

## 8. Instagram — later, not now

Leave `INSTAGRAM_MOCK=true`. The redirect URI is
`https://<your-render-url>/oauth/instagram/callback`; register it whenever you
start Track A. Nothing else in the codebase changes when it lands.

---

## Verify the whole set

```bash
alembic upgrade head          # already stamped at 0001 — should report nothing to do
curl https://<your-render-url>/health/ready
```

`/health/ready` checks Postgres, Redis, and that `ANTHROPIC_MODEL` and
`R2_PUBLIC_BASE_URL` are actually set. `/health` is liveness only and
deliberately does not touch Postgres — Render should not cycle your web service
because Neon had a blip.
