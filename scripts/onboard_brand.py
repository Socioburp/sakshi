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
    python scripts/onboard_brand.py --brand <uuid> --refs <folder|drive-url> \\
        --products <folder|drive-url> [--dry-run]

Either side takes a local folder or a Google Drive folder shared with "anyone
with the link" (needs GOOGLE_API_KEY). Re-run it whenever the team adds more
files: every file is identified by the hash of its own bytes, so a second run
stores only what is new and says what it skipped.

Exit codes: 0 everything landed; 1 the run finished but something was refused
(a photo too small or too soft, a reference the style pass could not read);
2 nothing was imported (bad folder, missing key, unknown brand) -- so a
half-import never passes for a success in a shell script or a checklist.
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
from sqlalchemy import select

from app.config import settings
from app.creative import brandkit, photo_quality, refstyle
from app.creative import logo as logo_analysis
from app.db.models import Brand, BrandAsset, BrandMemory
from app.db.session import session_scope
from app.integrations.storage import r2
from app.memory import embed

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
# Kinds the vision pass is allowed to file a photo under. 'reference' is not
# among them: only the --refs side produces those, and only here.
PHOTO_KINDS = ("product", "shop", "team", "other")

DRIVE_API = "https://www.googleapis.com/drive/v3/files"
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


def memory_ref(item: Item) -> str:
    return f"onboarding:{item.digest[:16]}"


def store(db, brand_id: uuid.UUID, item: Item) -> BrandAsset:
    url = r2.put(item.key, item.data, item.mime)
    row = BrandAsset(
        brand_id=brand_id,
        kind=item.kind,
        label=item.label,
        storage_key=item.key,
        url=url,
        mime=item.mime,
        width=item.width,
        height=item.height,
    )
    db.add(row)
    db.flush()
    return row


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


async def run(args: argparse.Namespace) -> int:
    brand_id = uuid.UUID(args.brand)
    with session_scope() as db:
        brand = db.get(Brand, brand_id)
        if brand is None:
            print(f"no brand {brand_id}", file=sys.stderr)
            return 2
        brand_name = brand.name
        known_keys = stored_keys(db, brand_id)
        known_refs = remembered_refs(db, brand_id)

    try:
        product_files = read_source(args.products) if args.products else []
        reference_files = read_source(args.refs) if args.refs else []
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

    notes = await label_photos(products.stored)
    described, refused = await read_references(references.stored)
    references.rejected.extend(refused.rejected)
    # A reference the style pass could not read is not stored either: the file
    # on its own teaches nobody anything, and next week's re-run should try it
    # again rather than treat it as done.
    unread = {note.name for note in refused.rejected}
    references.stored = [item for item in references.stored if item.name not in unread]
    fresh_references = [item for item in references.stored if not item.already_stored]

    kit = refstyle.aggregate([ref for _, ref in described]) if described else None

    print(f"Brand: {brand_name} ({brand_id})")
    if args.dry_run:
        print("DRY RUN -- nothing is written")
    _report(products, "Product photos")
    for note in notes:
        print(f"  note     {note}")
    _report(references, "Reference creatives")
    if not refstyle.available() and reference_files:
        print("  note     no vision model configured: the style pass was skipped")

    _line("Brand kit")
    if kit is None:
        print("  unchanged (no reference creative was read)")
    else:
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
    with session_scope() as db:
        for item in products.stored + fresh_references:
            store(db, brand_id, item)
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
                    embed.remember(
                        db,
                        brand_id=brand_id,
                        kind="style_anchor",
                        content=ref.as_prose(brand_name),
                        meta={**ref.as_meta(), "file": item.name},
                        source_ref=memory_ref(item),
                    )
                    written += 1
        except Exception as exc:  # noqa: BLE001 - the import stands; say what is missing
            written = 0
            memory_failed = str(exc)[:200]

    _line("Written")
    print(f"  {len(products.stored)} product photo(s), {len(fresh_references)} reference(s)")
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
        epilog="Both --refs and --products take a local folder or a Google Drive folder "
        "shared 'anyone with the link'. Safe to re-run: files already stored are skipped.",
    )
    ap.add_argument("--brand", required=True, help="brands.id (uuid)")
    ap.add_argument("--refs", help="folder or Drive link: finished creatives OUR team made")
    ap.add_argument("--products", help="folder or Drive link: raw photos of the real products")
    ap.add_argument(
        "--dry-run", action="store_true", help="report what would happen, write nothing"
    )
    args = ap.parse_args()
    if not args.refs and not args.products:
        ap.error("give at least one of --refs and --products")
    try:
        uuid.UUID(args.brand)
    except ValueError:
        ap.error(f"--brand {args.brand} is not a uuid")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
