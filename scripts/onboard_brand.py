#!/usr/bin/env python3
"""Seed a new brand from our own team's work. Run by SocioBurp staff, never by an owner.

Why this exists
---------------
Everything Sakshi knows about a brand, it learns from that brand: taste needs
five votes, the grid signature needs six approved posts, the catalogue needs
the owner to have sent photos. A client who paid this morning has none of
that, so their first creatives -- the ones they decide about us on -- are made
with every learned lane empty.

So we stop starting from nothing. When a brand is onboarded our designers
already produce five to ten finished creatives for it, and the client already
sends us raw photographs of the real products. This command takes both:

  the product photos   become brand_assets, which makes the FREE owner-photo
                       lane work from day one -- a real photograph of the real
                       product, composited, at zero image cost
  the reference set    becomes style anchors in brand_memory and a brand kit
                       on the brand: the layout family, the light, the colours
                       and a short list of standing rules

A reference creative is NEVER given to the image model and never composited
over. It carries its own headline and its own logo; a model shown one copies
the lettering, which is the one thing the whole picture path exists to
prevent. It is read once, for style, by app/creative/refstyle.py.

Usage
-----
    python scripts/onboard_brand.py --phone <the client's number> \\
        --kit <brand folder|drive-url> [--dry-run]

The brand folder holds 'references' and 'products'. Name the brand by the
number the client messages us from; --brand <uuid> is there for the rare
account with several brands, and --refs/--products still take the two
sides separately when a kit is laid out some other way.

Either side takes a local folder or a Google Drive folder shared with "anyone
with the link" (needs GOOGLE_API_KEY). Re-run it whenever the team adds more
files: every file is identified by the hash of its own bytes, so a second run
stores only what is new and says what it skipped.

Exit codes: 0 everything landed; 1 the run finished but something was refused
or could not be written (a photo too small or too soft, a file that belongs in
the other folder, a reference the style pass could not read, memories Voyage
would not embed); 2 nothing was imported
(bad folder, missing key, unknown brand) -- so a half-import never passes for
a success in a shell script or a checklist.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import mimetypes
import re
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.creative import brandkit, photo_quality, refstyle
from app.creative import logo as logo_analysis
from app.db.models import Account, Brand, BrandAsset, BrandMemory
from app.db.session import session_scope
from app.integrations.storage import r2
from app.memory import embed

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
# Kinds the vision pass is allowed to file a photo under. 'reference' is not
# among them: only the --refs side produces those, and only here.
PHOTO_KINDS = ("product", "shop", "team", "other")

DRIVE_API = "https://www.googleapis.com/drive/v3/files"
DRIVE_FOLDER_MIME = "application/vnd.google-apps.folder"
# One brand, one folder, two folders inside it. The team keeps Drive this way
# already, so --kit takes the brand folder and finds the sides itself: a
# designer pastes one link instead of two and cannot swap them over.
KIT_REFS = ("references", "reference", "refs")
KIT_PRODUCTS = ("products", "product", "raw", "raw photos")
DRIVE_FOLDER_IN_URL = re.compile(r"/folders/([A-Za-z0-9_-]{8,})")
DRIVE_ID_PARAM = re.compile(r"[?&]id=([A-Za-z0-9_-]{8,})")
DRIVE_PAGE = 200
HTTP_TIMEOUT = 60.0
# A photograph is a few megabytes. Anything this size is a scan, a PSD exported
# with every layer or a mistake, and reading it is how a run that should take
# two minutes runs out of memory on someone's laptop instead.
MAX_FILE_BYTES = 40_000_000


class SourceError(RuntimeError):
    """The folder cannot be read at all. Nothing is imported; the run stops."""


@dataclass(slots=True)
class SourceFile:
    name: str
    data: bytes


@dataclass(slots=True)
class Item:
    """A file that will be stored, with everything measured about it."""

    name: str
    digest: str
    data: bytes
    mime: str
    width: int | None
    height: int | None
    kind: str
    label: str | None
    key: str
    # True for a reference that is already in R2 but has no style anchor yet:
    # the file is not uploaded again, the style pass still runs on it.
    already_stored: bool = False


@dataclass(slots=True)
class Note:
    name: str
    reason: str


@dataclass
class Plan:
    stored: list[Item] = field(default_factory=list)
    rejected: list[Note] = field(default_factory=list)
    skipped: list[Note] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# where the files come from
# --------------------------------------------------------------------------- #
def is_url(spec: str) -> bool:
    return spec.strip().lower().startswith(("http://", "https://"))


def drive_folder_id(url: str) -> str:
    match = DRIVE_FOLDER_IN_URL.search(url) or DRIVE_ID_PARAM.search(url)
    if not match:
        raise SourceError(
            f"{url} is not a Google Drive FOLDER link. Open the folder in Drive and copy "
            "the address bar; it looks like https://drive.google.com/drive/folders/<id>."
        )
    return match.group(1)


def drive_list(folder_id: str, api_key: str) -> list[dict]:
    """Every file in a public folder. Raises SourceError when it is not public."""
    out: list[dict] = []
    token = None
    with httpx.Client(timeout=HTTP_TIMEOUT) as client:
        while True:
            params = {
                "q": f"'{folder_id}' in parents and trashed = false",
                "key": api_key,
                "fields": "nextPageToken, files(id,name,mimeType,size)",
                "pageSize": str(DRIVE_PAGE),
                "orderBy": "name",
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            }
            if token:
                params["pageToken"] = token
            resp = client.get(DRIVE_API, params=params)
            if resp.status_code in (401, 403, 404):
                raise SourceError(
                    f"Google Drive answered {resp.status_code} for folder {folder_id}. "
                    "An API key can only read a folder shared with 'anyone with the link' "
                    "-- open the folder, Share, General access, Anyone with the link, "
                    "Viewer. (It also means this: check GOOGLE_API_KEY is a key with the "
                    "Drive API enabled.)"
                )
            resp.raise_for_status()
            body = resp.json()
            out.extend(body.get("files") or [])
            token = body.get("nextPageToken")
            if not token:
                return out


def drive_fetch(file_id: str, api_key: str) -> bytes:
    with httpx.Client(timeout=HTTP_TIMEOUT) as client:
        resp = client.get(DRIVE_API + f"/{file_id}", params={"alt": "media", "key": api_key})
        resp.raise_for_status()
        return resp.content


def read_drive(url: str) -> list[SourceFile]:
    if not settings.google_api_key:
        raise SourceError(
            "GOOGLE_API_KEY is unset, so a Drive link cannot be read. Set it in .env "
            "(a plain API key with the Drive API enabled), or download the folder and "
            "pass the local path instead."
        )
    folder_id = drive_folder_id(url)
    listing = drive_list(folder_id, settings.google_api_key)
    files = [f for f in listing if _looks_like_image(f.get("name", ""), f.get("mimeType"))]
    if not files:
        raise SourceError(
            f"folder {folder_id} lists {len(listing)} item(s) and none of them is an image. "
            "Sub-folders are not read: point the command at the folder the files are in."
        )
    out = []
    for f in sorted(files, key=lambda f: f["name"]):
        if int(f.get("size") or 0) > MAX_FILE_BYTES:
            print(f"  skipped  {f['name']}  (over {MAX_FILE_BYTES // 1_000_000}MB, not downloaded)")
            continue
        out.append(SourceFile(name=f["name"], data=drive_fetch(f["id"], settings.google_api_key)))
    return out


def read_folder(spec: str) -> list[SourceFile]:
    root = Path(spec).expanduser()
    if not root.is_dir():
        raise SourceError(f"{root} is not a folder on this machine.")
    paths = sorted(p for p in root.iterdir() if p.is_file() and _looks_like_image(p.name, None))
    if not paths:
        raise SourceError(f"{root} holds no images ({', '.join(sorted(IMAGE_SUFFIXES))}).")
    out = []
    for p in paths:
        if p.stat().st_size > MAX_FILE_BYTES:
            print(f"  skipped  {p.name}  (over {MAX_FILE_BYTES // 1_000_000}MB, not read)")
            continue
        out.append(SourceFile(name=p.name, data=p.read_bytes()))
    return out


def _kit_children(spec: str) -> tuple[dict[str, str], bool]:
    """The folders inside one brand folder, by lower-cased name."""
    if is_url(spec):
        if not settings.google_api_key:
            raise SourceError(
                "GOOGLE_API_KEY is unset, so a Drive link cannot be read. Set it in .env "
                "(a plain API key with the Drive API enabled), or download the folder and "
                "pass the local path instead."
            )
        listing = drive_list(drive_folder_id(spec), settings.google_api_key)
        return {
            (f.get("name") or "").strip().lower(): f["id"]
            for f in listing
            if f.get("mimeType") == DRIVE_FOLDER_MIME
        }, True
    root = Path(spec).expanduser()
    if not root.is_dir():
        raise SourceError(f"{root} is not a folder on this machine.")
    return {p.name.strip().lower(): str(p) for p in root.iterdir() if p.is_dir()}, False


def split_kit(spec: str) -> tuple[str, str, str]:
    """One brand folder -> the two sides of its kit, and what was chosen.

    The team's Drive is one folder per brand with 'references' and 'products'
    inside it, so asking for the brand folder is asking for what they have.
    Two links typed on one line are two chances to pass the client's raw
    photographs as our own finished creatives, and that mistake is expensive:
    a finished post filed as a product photo is a picture the system builds a
    new post ON TOP of, so the client's first creative goes out carrying two
    headlines and two logos.
    """
    children, is_drive = _kit_children(spec)
    picked, chosen = {}, []
    for side, names in (("refs", KIT_REFS), ("products", KIT_PRODUCTS)):
        hit = next((n for n in names if n in children), None)
        if hit is None:
            inside = ", ".join(sorted(children)) or "nothing"
            raise SourceError(
                f"{spec} has no '{names[0]}' folder inside it (it holds: {inside}). "
                f"--kit wants one folder per brand with '{KIT_REFS[0]}' and "
                f"'{KIT_PRODUCTS[0]}' in it; pass --refs and --products yourself if "
                "the kit is laid out some other way."
            )
        target = children[hit]
        picked[side] = f"https://drive.google.com/drive/folders/{target}" if is_drive else target
        chosen.append(f"{side} <- {hit}")
    return picked["refs"], picked["products"], ", ".join(chosen)


def read_source(spec: str) -> list[SourceFile]:
    """The files behind either kind of --refs/--products argument, in name order."""
    return read_drive(spec) if is_url(spec) else read_folder(spec)


def _looks_like_image(name: str, mime: str | None) -> bool:
    if mime and mime.startswith("image/"):
        return True
    return Path(name).suffix.lower() in IMAGE_SUFFIXES


# --------------------------------------------------------------------------- #
# what happens to a file before anything is written
# --------------------------------------------------------------------------- #
def _stem_label(name: str) -> str:
    """The filename as a label a person would read: "coconut_oil-500ml.jpg" ->
    "coconut oil 500ml". It is what the free photo lane matches copy against,
    so it is worth having even when no vision model is configured."""
    words = re.split(r"[\W_]+", Path(name).stem, flags=re.UNICODE)
    return " ".join(w for w in words if w).strip()[:160]


def _prepare(file: SourceFile, brand_id: str, kind: str) -> Item:
    """Upright the pixels once and name the file by its own bytes.

    The key is keyed on the FOLDER the file came from, not on the kind: the
    vision pass may decide a photo is the shopfront rather than a product, and
    a key that moved with that decision would make the next run store the file
    a second time.
    """
    lane = "reference" if kind == "reference" else "photo"
    mime = mimetypes.guess_type(file.name)[0] or "image/jpeg"
    data, mime, dims = photo_quality.upright(file.data, mime)
    digest = hashlib.sha256(data).hexdigest()
    ext = "png" if "png" in (mime or "") else "jpg"
    return Item(
        name=file.name,
        digest=digest,
        data=data,
        mime=mime,
        width=dims[0] if dims else None,
        height=dims[1] if dims else None,
        kind=kind,
        label=_stem_label(file.name) or None,
        key=r2.onboarding_key(brand_id, lane, digest, ext),
    )


def plan_products(files: list[SourceFile], *, brand_id: str, known_keys: set[str]) -> Plan:
    """Which raw photos become assets, which are refused, and which are already in.

    A photo is refused here, with the reason, rather than stored: a soft or
    tiny photograph cannot be rescued by anything downstream, and the moment to
    ask the client for a retake is while we are still onboarding them.
    """
    plan = Plan()
    seen: dict[str, str] = {}
    for file in files:
        item = _prepare(file, brand_id, "product")
        if item.digest in seen:
            plan.skipped.append(Note(file.name, f"the same file as {seen[item.digest]}"))
            continue
        seen[item.digest] = file.name
        if item.key in known_keys:
            plan.skipped.append(Note(file.name, "already stored for this brand"))
            continue
        quality = photo_quality.assess(item.data)
        if not quality.ok:
            plan.rejected.append(Note(file.name, _why(quality)))
            continue
        plan.stored.append(item)
    return plan


def plan_references(
    files: list[SourceFile], *, brand_id: str, known_keys: set[str], known_refs: set[str]
) -> Plan:
    """Reference creatives are kept as they are; only bytes we cannot open are refused.

    They are not held to the photo bar: a reference is read for its layout and
    its light, and a 900px export of a post says both perfectly well.

    The file and its style anchor are two separate "have we got this already?"
    questions, and they have to be, or the first run made on a machine with no
    vision model would store the images and lock the brand kit out forever: the
    second run would see the files, call it done, and never read them.
    """
    plan = Plan()
    seen: dict[str, str] = {}
    for file in files:
        item = _prepare(file, brand_id, "reference")
        if item.digest in seen:
            plan.skipped.append(Note(file.name, f"the same file as {seen[item.digest]}"))
            continue
        seen[item.digest] = file.name
        if item.key in known_keys:
            if memory_ref(item) in known_refs:
                plan.skipped.append(Note(file.name, "already stored and already read"))
                continue
            item.already_stored = True
            plan.stored.append(item)
            continue
        quality = photo_quality.assess(item.data)
        if quality.verdict == "unreadable":
            plan.rejected.append(Note(file.name, "the file could not be opened as an image"))
            continue
        item.width, item.height = quality.width, quality.height
        plan.stored.append(item)
    return plan


def _why(quality) -> str:
    """The refusal in words staff can forward to the client."""
    return {
        "small": f"only {quality.width}x{quality.height}px "
        f"(under {photo_quality.MIN_EDGE_PX}px on the short edge); ask for the original file, "
        "sent as a document rather than a WhatsApp photo",
        "blurry": f"soft focus (softness {quality.softness:.2f}, the bar is "
        f"{photo_quality.SOFT_ABOVE}); ask for a retake with the phone braced and tapped to focus",
        "dark": "too dark; ask for one taken near a window",
        "blown_out": "washed out; ask for one out of direct flash or sun",
        "unreadable": "the file could not be opened as an image",
    }.get(quality.verdict, quality.verdict)


async def label_photos(items: list[Item]) -> list[str]:
    """What each photo IS, from the vision pass. Fail-soft, by design.

    Without a model configured (or when the call fails) the filename stem stays
    as the label and the kind stays 'product' -- the photos still import, which
    is the whole point of the free lane, and the run says the pass was skipped.
    """
    if not (settings.anthropic_api_key and settings.anthropic_model):
        return ["no vision model configured: photos kept their filenames as labels"]
    notes: list[str] = []
    for item in items:
        try:
            seen = await logo_analysis.describe_photo(item.data, item.mime)
        except Exception as exc:  # noqa: BLE001 - one bad call must not stop the import
            notes.append(f"{item.name}: vision pass failed ({str(exc)[:80]}), filename kept")
            continue
        kind = seen.get("kind")
        if kind in PHOTO_KINDS:
            # The product lane cuts out and re-stages anything filed as
            # 'product'. A plate, a set or glass cannot be cut cleanly, so it
            # is kept whole -- the same rule the WhatsApp intake applies.
            item.kind = "other" if kind == "product" and seen.get("cut_out_ok") is False else kind
        if seen.get("label"):
            item.label = str(seen["label"])[:160]
    return notes


async def screen_folders(products: Plan, references: Plan) -> list[str]:
    """Catch the two folder arguments the wrong way round, before anything is stored.

        --refs ./anaya/products --products ./anaya/references

    is two paths on one command line and nothing downstream notices. The
    quality pass measures size, focus and exposure, all of which a finished
    1080x1350 post sails through; the photo vision pass has no way to say "this
    one already has a headline on it" and answers kind=product for a post
    showing one jar. The row lands as kind 'product', which photoref accepts,
    and the client's first creative ships with two headlines and two logos --
    the one failure this whole package exists to prevent. The background gate
    never sees it, because that only ever inspects pictures the image model
    made.

    So each file is judged on what it IS, not on the folder a human typed it
    into: design laid on top belongs in --refs, a plain photograph belongs in
    --products, and a file in the wrong one is refused by name.

    Only a clear answer moves a file. An image nobody could judge -- no model
    configured, or a call that would not answer -- is kept and named in the
    summary. The guarantee that a reference is never composited over is the
    kind whitelist and holds regardless; this is a check on a typo, and a check
    on a typo that refused a valid onboarding would be the worse bug.
    """
    notes: list[str] = []
    if not (products.stored or references.stored):
        return []
    if not refstyle.available():
        return [
            "no vision model configured: the files were not checked for being in the wrong "
            "folder, so read the names above yourself"
        ]
    unchecked: list[str] = []

    kept: list[Item] = []
    for item in products.stored:
        seen = await refstyle.looks_finished(item.data)
        if seen is None:
            unchecked.append(item.name)
            kept.append(item)
        elif seen[0]:
            products.rejected.append(
                Note(
                    item.name,
                    f"design has been laid on top of this, so it is not a plain photograph "
                    f"({seen[1] or 'words laid on top'}). If our team made it, it belongs in "
                    "--refs -- check the two folders are not the wrong way round. If the client "
                    "added a price or a logo in an app, ask for the picture without it",
                )
            )
        else:
            kept.append(item)
    products.stored = kept

    kept = []
    for item in references.stored:
        # One already on file was screened on the run that stored it; it is
        # here only because its style anchor is still missing.
        seen = None if item.already_stored else await refstyle.looks_finished(item.data)
        if seen is None:
            if not item.already_stored:
                unchecked.append(item.name)
            kept.append(item)
        elif seen[0]:
            kept.append(item)
        else:
            why = seen[1] or "nothing laid on top"
            references.rejected.append(
                Note(
                    item.name,
                    f"this is a plain photograph, not one of our creatives ({why}). A raw "
                    "product photo belongs in --products, where the free photo lane can use it",
                )
            )
    references.stored = kept

    if unchecked:
        notes.append(
            f"{len(unchecked)} file(s) could not be checked for the folder mix-up "
            f"({', '.join(unchecked[:4])}{', ...' if len(unchecked) > 4 else ''}): look at them"
        )
    return notes


async def read_references(items: list[Item]) -> tuple[list[tuple[Item, refstyle.Reference]], Plan]:
    """The style pass over every reference, one at a time, refusing what it cannot read.

    With no vision model configured there is no pass to refuse: the files are
    kept, the brand kit is left alone, and the run says so. Everything else
    about the onboarding still works, which is why this one is fail-soft while
    the pass itself is fail-closed.
    """
    out: list[tuple[Item, refstyle.Reference]] = []
    refused = Plan()
    if not refstyle.available():
        return out, refused
    for item in items:
        try:
            ref = await refstyle.describe(item.data)
        except refstyle.ReferenceUnreadable as exc:
            refused.rejected.append(Note(item.name, str(exc)[:160]))
            continue
        # The label is what a human sees in the asset list, so it says what the
        # post IS rather than what the file was called on someone's laptop.
        item.label = ref.summary[:160]
        out.append((item, ref))
    return out, refused


# --------------------------------------------------------------------------- #
# writing it down
# --------------------------------------------------------------------------- #
def stored_keys(db, brand_id: uuid.UUID) -> set[str]:
    return set(
        db.scalars(select(BrandAsset.storage_key).where(BrandAsset.brand_id == brand_id)).all()
    )


def remembered_refs(db, brand_id: uuid.UUID) -> set[str]:
    return set(
        db.scalars(select(BrandMemory.source_ref).where(BrandMemory.brand_id == brand_id)).all()
    )


def earlier_references(db, brand_id: uuid.UUID) -> tuple[list[refstyle.Reference], list[Note]]:
    """Every reference of this brand's that has already been read, in words again.

    The house style is the style of the WHOLE set. Without this, the documented
    way of working -- add the two posts the designers just made to the folder,
    re-run -- would decide the kit from those two alone, because everything
    else is skipped as already read. A ten-post house style would be replaced
    by a two-post minority, silently, and the brand would get dumber the more
    work we gave it.

    Nothing has to be re-read for this: each anchor already carries its facts
    in meta. One that cannot be read back is named in the summary rather than
    dropped quietly -- it is a reference whose vote is missing from the count.
    """
    rows = db.scalars(
        select(BrandMemory).where(
            BrandMemory.brand_id == brand_id, BrandMemory.kind == "style_anchor"
        )
    ).all()
    refs: list[refstyle.Reference] = []
    unreadable: list[Note] = []
    for row in rows:
        meta = dict(row.meta or {})
        if meta.get("source") != refstyle.ANCHOR_SOURCE:
            continue  # a style anchor from the owner's own approvals, not from us
        try:
            refs.append(refstyle.from_meta(meta, row.content))
        except refstyle.ReferenceUnreadable as exc:
            unreadable.append(
                Note(str(meta.get("file") or row.source_ref or row.id), str(exc)[:120])
            )
    return refs, unreadable


def memory_ref(item: Item) -> str:
    return f"onboarding:{item.digest[:16]}"


def store(db, brand_id: uuid.UUID, item: Item) -> bool:
    """Write the asset. False when another run stored this file first.

    The read that decided this file was new happened before a folder was
    downloaded and a vision pass run over it, which is minutes earlier, so two
    staff onboarding the same brand at once both see it as new. Migration 0015
    makes (brand_id, storage_key) unique for onboarding keys, and losing that
    race is not an error: the file IS stored, by the other run, which is what
    was wanted. The savepoint is what keeps the rest of the import alive -- a
    failed insert poisons its transaction, so without one the first collision
    would take every asset after it down with it.

    R2 is written either way and deliberately so: the key is the content hash,
    so both runs put identical bytes at an identical key.
    """
    url = r2.put(item.key, item.data, item.mime)
    try:
        with db.begin_nested():
            db.add(
                BrandAsset(
                    brand_id=brand_id,
                    kind=item.kind,
                    label=item.label,
                    storage_key=item.key,
                    url=url,
                    mime=item.mime,
                    width=item.width,
                    height=item.height,
                )
            )
            db.flush()
    except IntegrityError:
        return False
    return True


# --------------------------------------------------------------------------- #
# the run
# --------------------------------------------------------------------------- #
def _line(title: str) -> None:
    print(f"\n{title}\n" + "-" * len(title))


def _report(plan: Plan, what: str) -> None:
    _line(what)
    for item in plan.stored:
        verb = "re-read " if item.already_stored else "stored  "
        print(f"  {verb} {item.name}  [{item.kind}] {item.label or ''}".rstrip())
    for note in plan.skipped:
        print(f"  skipped  {note.name}  ({note.reason})")
    for note in plan.rejected:
        print(f"  REFUSED  {note.name}  {note.reason}")
    if not (plan.stored or plan.skipped or plan.rejected):
        print("  nothing")


def brand_for_phone(db, phone: str) -> tuple[uuid.UUID | None, str]:
    """The brand behind a WhatsApp number, or why there isn't one.

    Nobody on the team knows a brand by its uuid. They know the client by the
    number the client messages us from, which IS the account: the webhook
    creates one keyed by that number on first contact, and the brand hangs off
    it. So the command should take what they have and look the rest up.

    Compared on digits alone, because a number is written +91 98765 43210 by a
    person and 919876543210 by Meta, and a kit refused over a space is a kit
    loaded late.
    """
    digits = re.sub(r"\D", "", phone)
    if not digits:
        return None, f"--phone {phone!r} holds no digits."
    acct = db.scalar(
        select(Account).where(func.regexp_replace(Account.wa_phone, r"\D", "", "g") == digits)
    )
    if acct is None:
        return None, (
            f"no account for {digits}. A client's number becomes an account the first "
            "time they message the bot, so their kit can only be loaded after they have "
            "said hello -- check the number, or wait for their first message."
        )
    brands = db.scalars(
        select(Brand).where(Brand.account_id == acct.id).order_by(Brand.created_at)
    ).all()
    if not brands:
        return None, f"account {digits} exists but has no brand yet."
    if len(brands) == 1:
        return brands[0].id, ""
    default = [b for b in brands if b.is_default]
    if len(default) == 1:
        return default[0].id, ""
    listing = "\n".join(f"    {b.id}  {b.name}" for b in brands)
    return None, (
        f"{digits} has {len(brands)} brands and no single default one. Pass --brand "
        f"with the one you mean:\n{listing}"
    )


async def run(args: argparse.Namespace) -> int:
    refs, products = args.refs, args.products
    if args.kit:
        try:
            refs, products, chosen = split_kit(args.kit)
        except SourceError as exc:
            print(f"{exc}", file=sys.stderr)
            return 2
        print(f"Kit: {chosen}")

    with session_scope() as db:
        if args.brand:
            brand_id = uuid.UUID(args.brand)
        else:
            brand_id, why = brand_for_phone(db, args.phone)
            if brand_id is None:
                print(why, file=sys.stderr)
                return 2
        brand = db.get(Brand, brand_id)
        if brand is None:
            print(f"no brand {brand_id}", file=sys.stderr)
            return 2
        brand_name = brand.name
        known_keys = stored_keys(db, brand_id)
        known_refs = remembered_refs(db, brand_id)
        earlier, unreadable_anchors = earlier_references(db, brand_id)

    try:
        product_files = read_source(products) if products else []
        reference_files = read_source(refs) if refs else []
    except SourceError as exc:
        print(f"{exc}", file=sys.stderr)
        return 2

    products = plan_products(product_files, brand_id=str(brand_id), known_keys=known_keys)
    references = plan_references(
        reference_files,
        brand_id=str(brand_id),
        known_keys=known_keys,
        known_refs=known_refs,
    )

    # Which folder each file actually belongs in, decided before a byte is
    # uploaded or a style anchor written: a file in the wrong one is refused,
    # not filed under a kind that makes it a picture to build on.
    screen_notes = await screen_folders(products, references)
    notes = await label_photos(products.stored)
    described, refused = await read_references(references.stored)
    references.rejected.extend(refused.rejected)
    # A reference the style pass could not read is not stored either: the file
    # on its own teaches nobody anything, and next week's re-run should try it
    # again rather than treat it as done.
    unread = {note.name for note in refused.rejected}
    references.stored = [item for item in references.stored if item.name not in unread]
    fresh_references = [item for item in references.stored if not item.already_stored]

    # The kit is re-decided only when this run actually read something new, but
    # it is decided over the whole set: the references read now plus the ones
    # already on file. A run that read nothing leaves the kit exactly as it is.
    whole_set = earlier + [ref for _, ref in described]
    kit = refstyle.aggregate(whole_set) if described else None

    print(f"Brand: {brand_name} ({brand_id})")
    if args.dry_run:
        print("DRY RUN -- nothing is written")
    _report(products, "Product photos")
    for note in notes:
        print(f"  note     {note}")
    _report(references, "Reference creatives")
    if not refstyle.available() and reference_files:
        print("  note     no vision model configured: the style pass was skipped")
    if screen_notes:
        _line("Which folder each file belongs in")
        for note in screen_notes:
            print(f"  note     {note}")

    _line("Brand kit")
    for note in unreadable_anchors:
        print(
            f"  note     a style anchor on file could not be counted: {note.name} ({note.reason})"
        )
    if kit is None:
        print("  unchanged (no reference creative was read)")
    else:
        print(
            f"  decided from {len(whole_set)} reference(s): {len(described)} read now, "
            f"{len(earlier)} already on file"
        )
        print(f"  layout family  {kit.layout}  {kit.counts['layout']}")
        print(f"  light          {kit.light}  {kit.counts['light']}")
        print(f"  colours        {', '.join(kit.palette)}")
        for lesson in kit.lessons:
            print(f"  rule           {lesson}")

    if args.dry_run:
        return 1 if (products.rejected or references.rejected) else 0

    # The files and the brand kit go in first, in their own transaction. The
    # memories are written after, in another: embedding talks to Voyage, and a
    # Voyage outage inside the same transaction would roll back the whole
    # import and leave the brand with nothing after a twenty-minute upload.
    raced = 0
    with session_scope() as db:
        for item in products.stored + fresh_references:
            if not store(db, brand_id, item):
                raced += 1
        if kit is not None:
            decided = brandkit.seed_from_references(db.get(Brand, brand_id), kit)

    written = skipped_memories = 0
    memory_failed = ""
    if settings.voyage_api_key:
        try:
            with session_scope() as db:
                for item, ref in described:
                    if memory_ref(item) in known_refs:
                        skipped_memories += 1
                        continue
                    try:
                        # Same race as the assets, same answer: the anchor is
                        # written, by the other run. A savepoint, so one
                        # collision does not roll back the anchors before it.
                        with db.begin_nested():
                            embed.remember(
                                db,
                                brand_id=brand_id,
                                kind="style_anchor",
                                content=ref.as_prose(brand_name),
                                meta={**ref.as_meta(), "file": item.name},
                                source_ref=memory_ref(item),
                            )
                    except IntegrityError:
                        skipped_memories += 1
                        continue
                    written += 1
        except Exception as exc:  # noqa: BLE001 - the import stands; say what is missing
            written = 0
            memory_failed = str(exc)[:200]

    _line("Written")
    print(f"  {len(products.stored)} product photo(s), {len(fresh_references)} reference(s)")
    if raced:
        print(
            f"  {raced} of those were already stored by another run of this command "
            "finishing at the same time; nothing is duplicated"
        )
    if memory_failed:
        print(f"  0 style anchor(s): {memory_failed}. Re-run to write them; nothing duplicates.")
    elif settings.voyage_api_key:
        print(f"  {written} style anchor(s) in brand_memory, {skipped_memories} already there")
    elif described:
        print(
            "  0 style anchors: VOYAGE_API_KEY is unset, and a memory written without it "
            "can never be read back. Set it and re-run -- the kit above is already stored."
        )
    if kit is not None:
        print(f"  brand kit: look {decided['look']}, shoot {decided['shoot']}")
        print(f"  palette: {decided['palette']}")
        print(f"  {len(decided['lessons'])} standing rule(s) on the brand")
    return 1 if (products.rejected or references.rejected or memory_failed) else 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Seed a brand from our team's reference creatives and the client's "
        "raw product photos. Staff only.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Name the brand by --phone (the number the client messages us from) or "
        "--brand (its uuid). Give the kit as one --kit brand folder holding 'references' "
        "and 'products', or as --refs and --products separately. Either takes a local "
        "folder or a Google Drive folder shared 'anyone with the link'. Safe to re-run: "
        "files already stored are skipped.",
    )
    who = ap.add_mutually_exclusive_group(required=True)
    who.add_argument("--phone", help="the client's WhatsApp number, however it is written")
    who.add_argument("--brand", help="brands.id (uuid), when a number has several brands")
    ap.add_argument("--kit", help="ONE brand folder holding 'references' and 'products'")
    ap.add_argument("--refs", help="folder or Drive link: finished creatives OUR team made")
    ap.add_argument("--products", help="folder or Drive link: raw photos of the real products")
    ap.add_argument(
        "--dry-run", action="store_true", help="report what would happen, write nothing"
    )
    args = ap.parse_args()
    if args.kit and (args.refs or args.products):
        ap.error("--kit already names both sides; drop --refs and --products")
    if not args.kit and not args.refs and not args.products:
        ap.error("give --kit, or at least one of --refs and --products")
    if args.brand:
        try:
            uuid.UUID(args.brand)
        except ValueError:
            ap.error(f"--brand {args.brand} is not a uuid")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
