"""Vendor the fonts the compositor sets type in.

Every render used to fetch its faces from Google Fonts. That made a third
party's uptime part of every creative -- and once a missing brand face became a
refused render (COMPOSE_REQUIRE_FONTS), a Google Fonts blip would have failed
every job in flight. It also cost ~0.5s of network wait per render.

This downloads the woff2 files for every face the product can set (the four
looks in brandkit.py, plus a Noto Sans face per Indic script in fonts.py), and
writes a stylesheet that points at them under the compositor's private host.
The result is committed: templates/fonts/ is part of the app, like a template.

    python scripts/fetch_fonts.py

All families here are SIL Open Font License 1.1, which permits bundling; the
licence text is written alongside them. Re-run to pick up upstream updates.
"""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.creative import brandkit, fonts  # noqa: E402

OUT = ROOT / "templates" / "fonts"
# A current Chrome UA: the CSS2 API serves woff2 + unicode-range subsets to it.
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)
_URL = re.compile(r"url\((https://fonts\.gstatic\.com/[^)]+\.woff2)\)")

OFL_NOTE = """The font files in this directory are redistributed under the SIL Open Font
License, Version 1.1 (https://openfontlicense.org). Each family's copyright is
held by its authors; see https://fonts.google.com/ for the specimen and licence
page of each: Poppins, Inter, Playfair Display, Fraunces, Manrope, and the Noto
Sans script families. Fetched by scripts/fetch_fonts.py.
"""


def families() -> dict[str, str]:
    """family -> weights, for everything the compositor may ask for."""
    out: dict[str, str] = {}
    for look in brandkit.LOOKS.values():
        out[look.heading] = fonts.HEADING_WEIGHTS
    for look in brandkit.LOOKS.values():
        # A face used as both heading and body needs the union of the weights.
        have = set(out.get(look.body, "").split(";")) - {""}
        out[look.body] = ";".join(sorted(have | set(fonts.BODY_WEIGHTS.split(";")), key=int))
    for family, _, _ in fonts.SCRIPT_BLOCKS:
        out[family] = fonts.SCRIPT_WEIGHTS
    out["Noto Sans"] = fonts.SCRIPT_WEIGHTS  # the last resort in fonts.css_stack
    return out


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    sheets: list[str] = []
    seen: dict[str, str] = {}
    with httpx.Client(headers={"User-Agent": UA}, timeout=60, follow_redirects=True) as http:
        for family, weights in families().items():
            href = f"{fonts.GOOGLE_CSS}?{fonts._family_param(family, weights)}&display=block"
            css = http.get(href)
            css.raise_for_status()
            text = css.text
            for url in dict.fromkeys(_URL.findall(text)):
                if url not in seen:
                    name = (
                        re.sub(r"[^a-z0-9]+", "-", family.lower()).strip("-")
                        + "-"
                        + hashlib.sha1(url.encode()).hexdigest()[:10]
                        + ".woff2"
                    )
                    data = http.get(url)
                    data.raise_for_status()
                    (OUT / name).write_bytes(data.content)
                    seen[url] = name
                text = text.replace(url, f"{fonts.LOCAL_HOST}/{seen[url]}")
            sheets.append(f"/* {family} */\n{text.strip()}\n")
            print(f"  {family:24s} {len(set(_URL.findall(css.text))):3d} files")
    (OUT / "fonts.css").write_text("\n".join(sheets), encoding="utf-8", newline="\n")
    (OUT / "families.txt").write_text("\n".join(sorted(families())) + "\n", encoding="utf-8")
    (OUT / "OFL-NOTICE.txt").write_text(OFL_NOTE, encoding="utf-8")
    total = sum(p.stat().st_size for p in OUT.glob("*.woff2"))
    print(f"{len(seen)} files, {total / 1_048_576:.1f} MB -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
