# The onboarding kit

Every new brand starts knowing nothing about itself. Sakshi learns a client's
taste from their votes (it needs five) and their grid from their approved posts
(it needs six), so for roughly the first week a brand-new client gets creatives
made with none of that — and that is the week they decide whether we were worth
the money.

The onboarding kit is how we stop starting from nothing. Before the client's
first creative we give the system two things our team already makes anyway:

1. **The reference set** — five to ten finished creatives our designers made
   for this brand. They are read for *style*, and they set the brand's kit.
2. **The product photos** — the client's own raw photographs of the real
   products. They become the brand's own photo library, which means many of
   their posts can be built on a real photograph of the real product, free,
   from day one.

One person runs one command and both land. This page is how.

---

## What to shoot, and how many

**Product photos: 8–15 is a good kit, 5 is the floor.**

| What | How many | Why |
|---|---|---|
| Each hero product, whole, on a plain surface | 1 each | This is the shot most posts use |
| The same product from a second angle | 1 each | Two posts about one product should not look like the same post |
| A close detail (the texture, the label, the stitching) | 2–3 | Carousels need a close slide |
| The shop or counter | 1–2 | "Visit us" posts, festival posts |
| The team or the owner at work | 1–2 | The posts that get the most saves |

Rules for the person holding the phone:

- **Full size, sharp, and bright.** Shoot in daylight, brace the phone, tap
  the product to focus. Send the originals — not screenshots, not
  WhatsApp-compressed copies. The command measures every photo and refuses
  anything under 600px on the short edge or visibly soft.
- **One product per photo**, unless the post is genuinely about a set.
- **Nothing written on the image.** No price stickers added in an app, no
  borders, no watermark, no logo pasted on. Those are added later, by us.
- **Plain, uncluttered backgrounds** wherever possible. A busy background
  survives, but a plain one gives the layout more places to put the words.

**Reference creatives: 5–10, and they must agree with each other.**

A good reference set is a set: the same brand doing the same thing several
times, so there is a house style to read. Ten posts that each look completely
different tell us nothing, and the command will say so — it only writes a
standing rule where more than half the set agrees.

Include the posts you would be happy for the next fifty to look like. Leave
out experiments, one-off festival specials in someone else's colours, and
anything a client rejected.

> **Reference creatives are read for STYLE. They are never copied.**
> A finished post already carries its own headline and its own logo. It is
> never composited over and never shown to the image model — an image model
> shown a post with lettering on it puts lettering on the next one, which is
> the single failure the whole picture pipeline exists to prevent. What the
> system takes from the set is the *description*: the layout family, where the
> words sit, the colours, the light, how the product is shown. The files
> themselves are kept only so a human can see what seeded the brand.

---

## The command

From the repo, with the app's environment loaded:

```
python scripts/onboard_brand.py --brand <brand-uuid> \
    --refs  ./kits/anaya/references \
    --products ./kits/anaya/products
```

Or straight from Drive, with no downloading:

```
python scripts/onboard_brand.py --brand <brand-uuid> \
    --refs  "https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUv" \
    --products "https://drive.google.com/drive/folders/1ZyXwVuTsRqPoNmLkJiHgF"
```

Either side can be a local folder or a Drive link, and you can mix them. Add
`--dry-run` to see exactly what would happen and write nothing.

**Before a Drive link will work:**

- `GOOGLE_API_KEY` must be set in the environment (a plain Google API key with
  the Drive API enabled — there is no sign-in and no service account).
- The folder must be shared **Anyone with the link → Viewer**. Open it in
  Drive → Share → General access → Anyone with the link. If it is not, the
  command says so and stops without importing anything.
- Point it at the folder the files are *in*. Sub-folders are not read.

You need the brand's id. It is the `brands.id` column — the same uuid that
appears in the admin view and in the R2 paths for that brand.

---

## What it prints

```
Brand: Anaya Foods (84ae388e-eea1-4f3d-86de-6ffd392e3bf0)

Product photos
--------------
  stored   coconut_oil-500ml.jpg  [product] cold pressed coconut oil 500ml
  stored   shopfront.jpg  [shop] the shop at evening
  skipped  kadai.jpg  (already stored for this brand)
  REFUSED  tiny_bottle.jpg  only 400x500px (under 600px on the short edge); ask
           for the original file, sent as a document rather than a WhatsApp photo

Reference creatives
-------------------
  stored   post1.jpg  [reference] A dark jar low in the frame, words on a cream panel.

Brand kit
---------
  layout family  frame_card  {'frame_card': 4, 'lower_third': 1}
  light          moody  {'moody': 4, 'bright_airy': 1}
  colours        #1F5B3D, #F3E7D3
  rule           their posts are built as frame_card -- keep new ones in that family
  rule           their posts set the words on a solid panel
  rule           their product is shown whole
  rule           their pictures are moody, one low light source and deep shadows

Written
-------
  2 product photo(s), 1 reference(s)
  5 style anchor(s) in brand_memory, 0 already there
  brand kit: look editorial, shoot moody
  palette: confirmed by the reference set
  4 standing rule(s) on the brand
```

Read it like this:

- **stored** — it is in, and the label in brackets is what the system thinks
  it is. A wrong label is worth fixing: it is what the free photo lane matches
  the owner's words against.
- **skipped** — already there, the same file twice in the folder, or over 40MB
  (that is a scan or a layered export, not a photograph). Normal.
- **REFUSED** — nothing was stored for it. The reason is written to be
  forwarded to the client more or less as it stands.
- **Brand kit** — what the reference set decided. The counts in braces show
  how much the set agreed; a rule is only written where more than half of it
  did.

**Exit codes**, for anyone scripting this: `0` everything landed, `1` the run
finished but something was refused or could not be written (read the summary
and re-run once it is fixed), `2` nothing was imported at all (bad folder,
missing key, unknown brand).

---

## When it refuses a photo

Nothing downstream can put back what the phone did not capture — a cutout of a
blurry jar is a blurry jar on a clean background — so a poor photo is refused
rather than stored, and the moment to ask for a retake is now, while we are
still onboarding the client.

| It says | Ask the client for |
|---|---|
| `only 400x500px (under 600px on the short edge)` | The original file. On WhatsApp, "Document" instead of "Photo", or AirDrop/Drive |
| `soft focus` | A retake: brace the phone against something, tap the product on the screen to focus, then shoot |
| `too dark` | One taken near a window, in the daytime |
| `washed out` | One out of direct sun and with the flash off |
| `the file could not be opened as an image` | A resend — it is probably a HEIC, a PDF or a broken download |

A refused photo is not stored at all, so once the client sends a better one
you simply put it in the folder and re-run.

### "this is a finished post, not a photograph"

The command also looks at *what each file is*, not just which folder you typed
it into, because the two paths sit next to each other on one line and swapping
them is easy:

| It says | What happened |
|---|---|
| `this is a finished post, not a photograph ... it belongs in --refs` | One of our own creatives was in the `--products` folder |
| `this is a plain photograph, not one of our creatives ... belongs in --products` | A raw client photo was in the `--refs` folder |

Move that file to the other folder and re-run. This matters more than it
sounds: a finished post filed as a product photo is a picture the system will
build a new post *on top of*, so the client's first creative would go out with
two headlines and two logos on it.

The check needs a vision model. Without one the run says `the files were not
checked for being in the wrong folder` and imports everything as it stands — so
on a machine with no model configured, read the file names in the summary
yourself.
A file the model could not judge is kept, not refused, and named in the summary
for you to look at.

---

## Adding more later

Re-run the same command with the same folder. Every file is identified by the
hash of its own bytes, so:

- files already stored are **skipped**, not duplicated;
- new files are stored;
- a reference that is on file but has never been read for style (for instance
  because the first run was made without a vision model configured) is read
  this time.

This means re-running is always safe, and is the normal way to work: add
photos to the Drive folder, run it again, read the summary. It is safe even if
two of you run it for the same brand at the same time — the database refuses
the second copy of a file and the run says `already stored by another run`.

Two things to know:

- **The brand kit is recomputed from the brand's whole reference set**, not
  from the files in the folder you passed this time. Everything already read
  for that brand is counted again alongside the new files, so a folder with
  two new posts in it cannot overrule the ten that seeded the brand. The
  summary says so in as many words: `decided from 12 reference(s): 2 read now,
  10 already on file`.
- **Style anchors need `VOYAGE_API_KEY`.** Without it the photos and files
  still import and the kit is still set, and the run tells you the anchors
  were not written. Set the key and run it again; nothing is duplicated.

---

## What the client sees

Nothing about this is visible to the owner. It is internal: no message is
sent, nothing appears in their chat. What they notice is that their first
creative already sits in their own colours, in the layout their own set uses,
built on a photograph of their own product — and that the bot occasionally
says so in its own words ("made it in the style your first set uses").

If you want to check a brand's seeded style later, look at `template_prefs`
on the brand (`look`, `family`, `shoot`, `lessons`) and at the `reference`
assets in R2 under `onboarding/<brand-id>/reference/`.
