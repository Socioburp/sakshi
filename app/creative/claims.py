"""Industry claim guards. What an agency's compliance desk would stop.

India's advertising self-regulator (ASCI) requires every claim to be
substantiated and holds health, food and finance to stricter rules; FSSAI
regulates food labelling and claims. A shop owner does not know these rules
and does not want to. So the rulebook lives here, keyed by the brand's
category, and runs server-side on every brief -- next to `never_say`, for the
same reason: the thing that decides whether a creative may ship should not be
something the model can talk itself past.

Two severities:
  block   -- the copy cannot ship as written; the agent gets the phrase, the
             reason and a rewrite, and tries again. No credit is spent.
  advise  -- ships, but the agent is told to soften it next time.

A brand can whitelist a claim it can actually back ("FSSAI licensed", "ISO
9001") with update_brand(substantiated=[...]); those phrases pass.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.creative.photoreal import _has_word


@dataclass(slots=True)
class Violation:
    phrase: str
    rule: str  # short id
    severity: str  # block | advise
    why: str
    rewrite: str

    def as_dict(self) -> dict[str, str]:
        return {
            "phrase": self.phrase,
            "severity": self.severity,
            "why": self.why,
            "rewrite": self.rewrite,
        }


# (pattern, rule id, severity, why, rewrite). Patterns are matched on the
# lower-cased copy with word boundaries; Hinglish spellings are included
# where owners actually write them.
_Rule = tuple[str, str, str, str, str]

GENERAL: list[_Rule] = [
    (
        r"\b(no\.?\s?1|number\s?1|#1|india'?s best|world'?s best|best in (the )?(city|town|india|world))\b",  # noqa: E501
        "superlative",
        "advise",
        "ASCI: a superlative needs substantiation (a survey, an award).",
        "say what is specific and true: 'since 1998', '2,000+ customers', 'family recipe'",
    ),
    (
        r"\b(lowest price guaranteed|cheapest in (town|city|india))\b",
        "price_superlative",
        "advise",
        "ASCI: a price superlative must be provable against competitors.",
        "'honest prices' or the actual price",
    ),
    (
        r"\b(100\s?%\s?guaranteed|guaranteed results?|results? guaranteed|guarantee(d)? (100|hundred)\s?%)\b",  # noqa: E501
        "guarantee",
        "block",
        "ASCI: an absolute guarantee is a claim the owner must be able to honour in law.",
        "describe the real promise: 'replacement if not happy', 'try it once'",
    ),
    (
        r"\b(free\s*\*|\*\s*free|conditions apply|t&c apply)\b",
        "free_asterisk",
        "advise",
        "ASCI: 'free' must be free; a starred condition is the classic complaint.",
        "state the condition in plain words in the same line",
    ),
]

HEALTH: list[_Rule] = [
    (
        r"\b(cure[sd]?|heal[sd]?|treat[sd]?|ilaa?j|jad se khatam|permanent (cure|solution|removal))\b",  # noqa: E501
        "cure",
        "block",
        "ASCI health guidelines / Drugs & Magic Remedies Act: no cure, treat or heal claims "
        "for cosmetics or food.",
        "'helps', 'for', 'made for' -- describe the use, not a medical outcome",
    ),
    (
        r"\b(clinically (proven|tested)|dermatologist (recommended|tested|approved)|doctor recommended|scientifically proven)\b",  # noqa: E501
        "clinical",
        "block",
        "ASCI: needs a real study or named endorsement on file; unproven by default.",
        "'made with <ingredient>', 'tested on ourselves for two years', a real customer line",
    ),
    (
        r"\b(100\s?%\s?safe|no side ?effects|zero side ?effects|completely safe|chemical[- ]free)\b",  # noqa: E501
        "safety_absolute",
        "block",
        "ASCI: absolute safety and 'chemical-free' claims are unsubstantiable.",
        "'gentle', 'made without parabens' (only if true), 'patch-test recommended'",
    ),
    (
        r"\b(fair(ness)? (cream|skin|in \d+ days)|skin ?whitening|gora|goree|lighten(s|ing)? (your )?skin)\b",  # noqa: E501
        "skin_tone",
        "block",
        "ASCI 2014 guideline: no skin-tone / fairness benefit claims.",
        "'even tone', 'glow', 'hydration' -- benefits that are not a skin colour",
    ),
    (
        r"\b(weight loss|lose \d+ ?kg|fat (burn|loss)|slimming)\b",
        "weight_loss",
        "advise",
        "ASCI: weight-loss claims need clinical support; usually rejected.",
        "'part of an active routine', 'light', 'high-protein'",
    ),
    (
        r"\b(immunity booster|boosts? immunity|anti[- ]?viral|kills? (99|100)\s?%)\b",
        "immunity",
        "block",
        "FSSAI/ASCI (post-2020 advisories): immunity and anti-viral claims are not allowed.",
        "'traditional recipe', 'made with tulsi and ginger' -- ingredients, not effects",
    ),
]

FOOD: list[_Rule] = [
    (
        r"\b(100\s?%\s?organic|certified organic|jaivik)\b",
        "organic",
        "block",
        "FSSAI: 'organic' needs Jaivik Bharat / NPOP certification on the label.",
        "'farm-fresh', 'no chemicals used in our fields' (only if true), 'seasonal'",
    ),
    (
        r"\b(sugar[- ]free|zero sugar|fat[- ]free|cholesterol[- ]free|diabetic[- ]friendly)\b",
        "nutrition_claim",
        "advise",
        "FSSAI: nutrition claims have exact thresholds; 'less sugar' is safer than 'sugar-free'.",
        "'less sugar', 'made with jaggery', 'lighter'",
    ),
    (
        r"\b(medicinal|therapeutic|ayurvedic medicine|cures? (cough|cold|acidity|diabetes))\b",
        "food_medical",
        "block",
        "FSSAI: food cannot carry medicinal claims.",
        "'traditional', 'home-style', 'grandmother's recipe'",
    ),
    (
        r"\b(100\s?%\s?pure|asli 100\s?%|100\s?%\s?natural)\b",
        "purity_absolute",
        "advise",
        "ASCI/FSSAI: '100% pure/natural' invites a complaint unless lab-tested.",
        "'single-origin', 'cold-pressed', 'no added colour' (only if true)",
    ),
]

FINANCE: list[_Rule] = [
    (
        r"\b(guaranteed|assured|fixed|sure) returns?\b|\brisk[- ]free\b|\bdouble your money\b|\bno loss\b",  # noqa: E501
        "returns_guarantee",
        "block",
        "SEBI/RBI/ASCI: no assured-return or risk-free claims for investments.",
        "'past performance', 'as per plan document', 'talk to us for details'",
    ),
    (
        r"\b(loan (approved|guaranteed) in \d+ (min|minutes|hours?)|instant loan guaranteed|no documents?)\b",  # noqa: E501
        "loan_promise",
        "advise",
        "RBI digital lending guidelines: approval promises and 'no documents' invite action.",
        "'quick process', 'minimal paperwork'",
    ),
]

EDUCATION: list[_Rule] = [
    (
        r"\b(100\s?%\s?(placement|selection|result)|guaranteed (job|placement|selection|rank|admission)|rank guaranteed|selection guaranteed)\b",  # noqa: E501
        "placement_guarantee",
        "block",
        "ASCI education guidelines: no guaranteed jobs, ranks or admissions.",
        "'92% of last batch placed' (only with records), 'small batches', 'weekly tests'",
    ),
]

REAL_ESTATE: list[_Rule] = [
    (
        r"\b(guaranteed (appreciation|returns?)|prices? will (double|rise)|assured rental)\b",
        "appreciation",
        "block",
        "RERA/ASCI: no assured appreciation or rental claims.",
        "location facts: 'walking distance to metro', 'RERA no. on request'",
    ),
]

# category keyword -> rulebook(s), matched as whole words on the brand category
CATEGORY_RULES: list[tuple[tuple[str, ...], list[_Rule]]] = [
    (
        (
            "skin",
            "skincare",
            "cosmetic",
            "beauty",
            "cream",
            "serum",
            "soap",
            "hair",
            "ayurved",
            "ayurvedic",
            "wellness",
            "clinic",
            "hospital",
            "pharma",
            "pharmacy",
            "supplement",
            "nutrition",
            "gym",
            "fitness",
            "yoga",
            "spa",
            "salon",
            "dental",
            "dentist",
            "physio",
            "herbal",
        ),
        HEALTH,
    ),
    (
        (
            "food",
            "restaurant",
            "cafe",
            "bakery",
            "sweet",
            "sweets",
            "oil",
            "oils",
            "spice",
            "spices",
            "masala",
            "organic",
            "farm",
            "dairy",
            "milk",
            "pickle",
            "pickles",
            "snack",
            "snacks",
            "tea",
            "coffee",
            "juice",
            "honey",
            "ghee",
            "grocery",
            "kirana",
            "tiffin",
            "catering",
            "chocolate",
        ),
        FOOD,
    ),
    (
        (
            "loan",
            "loans",
            "insurance",
            "invest",
            "investment",
            "mutual fund",
            "chit",
            "trading",
            "forex",
            "crypto",
            "finance",
            "financial",
            "nbfc",
            "wealth",
        ),
        FINANCE,
    ),
    (
        (
            "coaching",
            "tuition",
            "tuitions",
            "academy",
            "institute",
            "school",
            "classes",
            "training",
            "course",
            "courses",
            "education",
            "college",
        ),
        EDUCATION,
    ),
    (
        (
            "property",
            "properties",
            "builder",
            "builders",
            "plot",
            "plots",
            "flat",
            "flats",
            "real estate",
            "realty",
        ),
        REAL_ESTATE,
    ),
]

_WS = re.compile(r"\s+")


def _fold(text: str) -> str:
    """Lower-case and collapse whitespace; punctuation stays so '100%', 'No.1'
    and 'T&C' still match their patterns."""
    return _WS.sub(" ", (text or "").lower()).strip()


def rules_for(category: str | None) -> list[_Rule]:
    cat = (category or "").lower()
    out: list[_Rule] = list(GENERAL)
    for keys, book in CATEGORY_RULES:
        if any(_has_word(cat, k) for k in keys):
            out.extend(book)
    return out


def _copy_of(brief: Any) -> str:
    parts = [
        getattr(brief, "headline", None),
        getattr(brief, "subhead", None),
        getattr(brief, "cta", None),
        getattr(getattr(brief, "caption", None), "body", None),
        getattr(brief, "alt_text", None),
    ]
    for s in getattr(brief, "slides", None) or []:
        parts += [getattr(s, "headline", None), getattr(s, "subhead", None)]
    return " ".join(p for p in parts if p)


def check(
    brief: Any, category: str | None, substantiated: list[str] | None = None
) -> list[Violation]:
    """Every claim in the brief's copy that the rulebook objects to.

    `substantiated` phrases (the owner has the certificate) are exempt.
    Ordered: blocks first, then advice; each phrase reported once.
    """
    text = _fold(_copy_of(brief))
    if not text:
        return []
    ok = {_fold(s) for s in (substantiated or []) if s}
    found: list[Violation] = []
    seen: set[str] = set()
    for pattern, rule, severity, why, rewrite in rules_for(category):
        for m in re.finditer(pattern, text, flags=re.IGNORECASE):
            phrase = m.group(0).strip()
            key = _fold(phrase)
            if key in seen or any(key in s or s in key for s in ok if s):
                continue
            seen.add(key)
            found.append(Violation(phrase, rule, severity, why, rewrite))
    found.sort(key=lambda v: 0 if v.severity == "block" else 1)
    return found


def blocking(violations: list[Violation]) -> list[Violation]:
    return [v for v in violations if v.severity == "block"]


def prompt_block(category: str | None) -> str:
    """The rules the agent should know before it writes, in one paragraph."""
    books = []
    cat = (category or "").lower()
    for keys, book in CATEGORY_RULES:
        if any(_has_word(cat, k) for k in keys):
            books.append(book)
    if not books:
        return (
            "## Claims\nEvery claim must be something the owner can prove. No 'No.1', "
            "'best in city', 'guaranteed' unless they said so and can show it. The server "
            "blocks unprovable claims before the creative is shown."
        )
    lines = ["## Claims (this industry has rules; the server enforces them)"]
    for book in books:
        for _, rule, severity, why, rewrite in book:
            if severity == "block":
                lines.append(f"- {rule.replace('_', ' ')}: {why} Instead: {rewrite}.")
    lines.append(
        "If the owner has a certificate (FSSAI, ISO, a clinical study), record the exact "
        "phrase with update_brand(substantiated=[...]) and it is allowed."
    )
    return "\n".join(lines)
