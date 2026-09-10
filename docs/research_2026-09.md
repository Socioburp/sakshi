# Research notes — image generation, product photos, prompting (Sep 2026)

What was looked at, what was adopted, and how sure each claim is.
Confidence is the author's estimate that the statement is true today; the
source is where to check it.

## Image generation vendors (adopted: `app/creative/imagegen/providers.py`)

| Claim | Confidence | Source |
|---|---|---|
| fal.ai FLUX.1 [schnell]: `POST https://fal.run/fal-ai/flux/schnell`, `Authorization: Key`, custom `image_size: {width,height}`, reply `images[0].url` | 0.95 | [fal.ai schnell API](https://fal.ai/models/fal-ai/flux/schnell/api), [fal docs](https://fal.ai/docs/model-api-reference/image-generation-api/flux-schnell) |
| fal price: $0.003 per megapixel, commercial use included | 0.9 | [fal docs](https://fal.ai/docs/model-api-reference/image-generation-api/flux-schnell) |
| fal queue API: `POST https://queue.fal.run/{model}` → `status_url`/`response_url`; statuses IN_QUEUE / IN_PROGRESS / COMPLETED | 0.9 | [fal queue docs](https://fal.ai/docs/model-endpoints/queue) |
| fal FLUX.2 [klein] 4B endpoint `fal-ai/flux-2/klein/4b`, $0.009 per megapixel | 0.8 | [fal klein guide](https://fal.ai/learn/devs/flux-2-klein-prompt-guide) |
| Replicate official model: `POST https://api.replicate.com/v1/models/{owner}/{name}/predictions`, `Prefer: wait=60`, `Authorization: Bearer`, poll `GET /v1/predictions/{id}`; statuses starting/processing/succeeded/failed/canceled | 0.95 | [Replicate HTTP reference](https://replicate.com/docs/reference/http), [create a prediction](https://replicate.com/docs/topics/predictions/create-a-prediction) |
| Replicate flux-schnell price: "$3.00 / thousand output images" | 0.9 | [Replicate pricing](https://replicate.com/pricing) |
| Replicate flux-schnell input: `aspect_ratio` ∈ {1:1, 16:9, 21:9, 3:2, 2:3, 4:5, 5:4, 3:4, 4:3, 9:16, 9:21} (4:5 renders 896×1088 at 1MP), `megapixels` ∈ {"1","0.25"}, `output_format` ∈ {webp,jpg,png} (default webp), `output_quality` default 80, `go_fast` default true, schnell `num_inference_steps` ≤ 4 | 0.95 | [replicate/cog-flux predict.py](https://github.com/replicate/cog-flux) (Apache-2.0), [Replicate flux-schnell API](https://replicate.com/black-forest-labs/flux-schnell/api) |
| FLUX.1 [schnell] weights are Apache-2.0 (commercial use allowed) | 0.95 | [Replicate model page](https://replicate.com/black-forest-labs/flux-schnell) |
| BFL direct API: `POST https://api.bfl.ai/v1/{model}` with `x-key`, width/height multiples of 16 and ≤4MP, reply `{id, polling_url}`, poll → `status: Pending|Ready|Error`, `result.sample` URL expires in 10 min | 0.9 | [black-forest-labs/skills](https://github.com/black-forest-labs/skills) (bfl-api references, MIT) |
| BFL prices: FLUX.2 [klein] 4B 1.4c first MP, [pro] 3c, [max] 7c; FLUX.2 [dev] is non-commercial | 0.85 | same repo, `model-selection-guide.md` |
| BFL rate limit: 24 concurrent requests | 0.8 | same repo, `rate-limiting.md` |

Decision: three adapters, one contract, chosen by `IMAGEGEN_PROVIDER`.
Schnell on fal or Replicate is the cheap default (≈₹0.25–0.40 per background);
FLUX.2 [klein]/[pro] through BFL is the quality tier. Every adapter downloads
the picture and refuses a blank, tiny or undecodable one, so a wrong payload
fails loudly instead of shipping a flat background.

## Prompting (adopted: `flux_prompt()`, system prompt)

| Claim | Confidence | Source |
|---|---|---|
| "Most FLUX models do not support negative prompts"; describe the positive replacement instead | 0.95 | [BFL Technical Parameters](https://bfl.mintlify.app/guides/prompting_unified_technical), [BFL skills `core-principles.md`](https://github.com/black-forest-labs/skills) |
| Structure `[Subject] + [Action] + [Style] + [Context] + [Lighting] + [Technical]`; prose beats keyword lists; front-load the subject; 30–80 words; lighting has the biggest effect | 0.9 | BFL skills `core-principles.md`, [BFL prompt basics](https://bfl.mintlify.app/guides/prompting_unified_basics) |
| Hex colours in prompts (`#RRGGBB` with the colour name) are honoured | 0.7 (BFL states it; not measured here) | BFL skills `hex-color-prompting.md` |

Adopted: the brief's negative list is folded into positive phrasing per
vendor (`flux_prompt`), the system prompt tells the model to front-load the
subject and never write negatives. Not adopted yet: brand hex colours in the
background prompt (worth an A/B once a vendor key exists).

## Product photos (adopted: `photo_quality.py`, earlier `product.py`)

| Claim | Confidence | Source |
|---|---|---|
| Variance of the Laplacian is the standard cheap blur measure | 0.9 | Pech-Pacheco et al. 2000; widely reproduced (e.g. pyimagesearch blur detection) |
| rembg default `bria-rmbg` is not licensed for commercial use; `isnet-general-use` is | 0.85 | [danielgatis/rembg](https://github.com/danielgatis/rembg) model table |
| BiRefNet (MIT) is sharper but needs >4GB RAM on CPU (OOM-killed at 1024px here) | 0.9 (measured) | this repo's product-lane notes |

## Type for Indian scripts (adopted: `app/creative/fonts.py`)

| Claim | Confidence | Source |
|---|---|---|
| Poppins covers Latin and Devanagari only; Tamil, Kannada, Telugu, Malayalam, Bengali, Gujarati, Gurmukhi, Odia need their own faces | 0.9 | Google Fonts family pages (Poppins: "Latin, Devanagari") |
| Noto Sans <Script> faces exist for every one of those scripts and are OFL-licensed | 0.95 | fonts.google.com/noto |
| `display=block` makes the browser wait for the real face rather than paint a fallback (up to its block period) | 0.9 | CSS Fonts `font-display` spec / Google Fonts `display` parameter |

Adopted: the compositor reads the copy, names the scripts in it, loads one
Noto face per script in its own stylesheet request (a brand face that is not
on Google Fonts must not take the Kannada face down with it), puts them in
the font-family stack, and opens up the display leading / drops the negative
tracking whenever an Indic script is present -- Devanagari marks sit above
and below the line and collided with the Latin line at the tight leading.
Measured here: a Kannada/Tamil/Hindi creative renders in 1.6s including
browser start-up; the wait for stylesheets and webfonts is bounded at 6s each.

## Expertise round (adopted: `claims.py`, `copybook.py`, `shotlist.py`, `brandkit.py`, grid-safe padding, `plan.py`)

| Claim | Confidence | Source |
|---|---|---|
| Instagram's profile grid now shows posts cropped to 3:4 (since Jan 2025), so the outer strips of a 1:1 or 4:5 post are hidden on the profile; 9:16 loses a band top and bottom | 0.85 | Instagram's own announcement of the 3:4 grid (Jan 2025), reproduced by Later / Hootsuite / Buffer grid guides |
| Single images lost reach and engagement year on year while Reels drive the most interactions and carousels the most saves (Metricool 2026 Instagram study) | 0.75 (one vendor's sample, large but self-selected) | [Metricool Instagram study 2026](https://metricool.com/instagram-study/) |
| ASCI Code: superlatives, "guaranteed", "cure", "clinically proven" and fairness/skin-tone claims need substantiation or are barred; FSSAI bars "organic"/nutrition claims without certification; SEBI/RBI/RERA bar assured-return and unregistered-project advertising | 0.85 | [ASCI Code for Self-Regulation](https://www.ascionline.in/the-asci-code/), FSSAI Advertising & Claims Regulations 2018, SEBI/RBI advertisement codes, RERA s.3 |
| Saves and shares are the strongest ranking signals on Instagram; "sends per reach" is named by Instagram's head as a key signal | 0.8 | Adam Mosseri, Instagram (2024 ranking explainers); consistent with the Metricool study |

## Instagram Insights (adopted: `app/integrations/instagram/insights.py`, `app/insights/performance.py`)

| Claim | Confidence | Source |
|---|---|---|
| Media insights: `GET /{ig-media-id}/insights?metric=...`; FEED metrics include `comments, follows, likes, profile_activity, profile_visits, reach, saved, shares, total_interactions, views`; REELS include `comments, likes, reach, saved, shares, total_interactions, views, ig_reels_avg_watch_time, ig_reels_video_view_total_time` | 0.9 | [Meta: Instagram media insights reference](https://developers.facebook.com/docs/instagram-platform/reference/instagram-media/insights) |
| `plays`, `clips_replays_count`, `ig_reels_aggregated_all_plays_count` deprecated for v22.0 and for all versions on 21 April 2025; `impressions` deprecated v22+ for media created after 2 July 2024; `views` introduced across media and user insights on 21 Jan 2025 | 0.9 | same reference; [Instagram Platform changelog](https://developers.facebook.com/docs/instagram-platform/changelog) |
| Insights data can be delayed up to 48 hours; stored up to 2 years; not available for album (carousel) children; story metrics live 24h | 0.9 | Meta media insights reference |
| Insights APIs for media and user objects became available on the Instagram API with Instagram Login on 21 Jan 2025; the permission is `instagram_business_manage_insights` and needs App Review | 0.85 | changelog (21 Jan 2025); [Meta permissions reference](https://developers.facebook.com/docs/permissions/) |
| User insights: `GET /{ig-user-id}/insights` with `period=day`, `metric_type=total_value`, `since/until`; `follower_count` and `online_followers` need 100+ followers; at most 30 days per request | 0.8 | [Meta: IG user insights reference](https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/insights) |
| `/{ig-user-id}/media` returns `id, media_type, media_product_type, timestamp, permalink, caption, like_count, comments_count` with cursor paging | 0.85 | Meta IG media reference (fields list) |

Adopted: the sync reads at most 30 posts a day per brand (about 35 calls,
inside the 200-per-user-per-hour platform limit), re-reads a post every 6h
while it is under a week old, every 3 days to a month, then fortnightly;
`follows`/`profile_visits` are requested for feed posts only, with a
core-metric fallback when a post refuses a metric. The insights scope is
added to the connect link only when `IG_INSIGHTS_ENABLED=true`, because an
unapproved scope fails the whole login dialog. Engagement is weighted
likes 1, comments 2, saves 3, shares 3, follows 5, divided by reach; a post
that beats the brand's median by 1.5x, once ten days old, earns a follow-up
idea. Below four measured posts nothing is said.

## Considered, not adopted (yet)

- **IC-Light / FLUX.2 image-to-image relighting** (lllyasviel/IC-Light, Apache-2.0;
  fal `iclight-v2`): re-lights a cut-out product into a scene. Strong, but it
  redraws product pixels, which this product promises never to do. Revisit
  as an opt-in "scene" mode with a side-by-side approval.
- **Real-ESRGAN** (BSD-3) upscaling for small owner photos: needs a GPU or
  a hosted endpoint; the photo gate asks for a bigger photo instead.
- **FLUX.2 [flex] typography**: text is composited in real fonts here, so
  in-model typography is unnecessary.
