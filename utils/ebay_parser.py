"""
utils/ebay_parser.py — Parse eBay variation names into structured card data

Your variation names follow these patterns:
    "153/197 Melmetal ex"
    "190/197 Ortega Reverse Holo RH"
    "198/197 Gloom"              ← above base = ultra rare
    "044/197 Charmander RH"
    "SVP 044 Charmander Black Star Promo"   ← promo pattern
    "SWSH001 Pikachu"                        ← SWSH promo pattern

This module extracts:
    card_number   → "153"
    set_total     → "197"
    card_name     → "Melmetal ex"
    variant_type  → "Reverse Holo" | "Normal" | etc.
    card_type     → "common" | "reverse_holo" | "holo" | "ultra_rare" | "promo"
    set_override  → set name override for promos (e.g. "Scarlet & Violet Black Star Promos")
"""

import re

# ── Suffix tokens that indicate variant/finish ────────────────────────────────
VARIANT_PATTERNS = [
    (r"\bRH\b",                   "Reverse Holo"),
    (r"\bReverse\s+Holo\b",       "Reverse Holo"),
    (r"\bReverse\b",              "Reverse Holo"),
    (r"\bReverse\s+H\b",          "Reverse Holo"),   # truncated "Reverse H"
    (r"\bCosmos\s+Holo\b",        "Cosmos Holo"),
    (r"\bMaster\s+Ball\b",        "Master Ball Pattern"),
    (r"\bPoke\s+Ball\b",          "Poke Ball Pattern"),
    (r"\bHolo\b",                 "Holo"),
]

# ── Promo set patterns ────────────────────────────────────────────────────────
# Maps regex → (set_name, set_code_prefix)
PROMO_PATTERNS = [
    # SVP 044 Charmander Black Star Promo
    # SVP044 Charmander
    (
        re.compile(r'^SVP\s*0*(\d+)\s+(.+?)(?:\s+Black\s+Star\s+Promo)?$', re.IGNORECASE),
        "Scarlet & Violet Black Star Promos",
    ),
    # SWSH001 Pikachu
    (
        re.compile(r'^SWSH0*(\d+)\s+(.+)$', re.IGNORECASE),
        "SWSH Black Star Promos",
    ),
    # SM001 Pikachu
    (
        re.compile(r'^SM0*(\d+)\s+(.+)$', re.IGNORECASE),
        "SM Black Star Promos",
    ),
    # XY001 Pikachu
    (
        re.compile(r'^XY0*(\d+)\s+(.+)$', re.IGNORECASE),
        "XY Black Star Promos",
    ),
    # BW001 Pikachu
    (
        re.compile(r'^BW0*(\d+)\s+(.+)$', re.IGNORECASE),
        "BW Black Star Promos",
    ),
]


# ── Card-type classifier ──────────────────────────────────────────────────────
def classify_card_type(card_number: int, set_total: int, variant_type: str, card_name: str = "") -> str:
    if card_number > set_total:
        return "ultra_rare"
    vt = (variant_type or "").lower()
    if "reverse" in vt:
        return "reverse_holo"
    if "holo" in vt or "cosmos" in vt:
        return "holo"
    # ex, V, VMAX, VSTAR, GX are holofoil by definition
    name_lower = (card_name or "").lower()
    if any(name_lower.endswith(s) for s in (" ex", " v", " vmax", " vstar", " gx", " gx tag team")):
        return "holo"
    return "common"


# ── Main parser ───────────────────────────────────────────────────────────────
def parse_variation_name(variation_name: str, listing_title: str = "") -> dict:
    """
    Parse a single eBay variation name string.

    Returns a dict:
    {
        "raw":          "190/197 Ortega Reverse Holo RH",
        "card_number":  "190",
        "set_total":    "197",
        "card_name":    "Ortega",
        "variant_type": "Reverse Holo",
        "card_type":    "reverse_holo",
        "set_override": None,            # only set for promos
        "parse_ok":     True,
    }
    """
    result = {
        "raw":          variation_name,
        "card_number":  None,
        "set_total":    None,
        "card_name":    None,
        "variant_type": "Normal",
        "card_type":    "common",
        "set_override": None,
        "source_type":  None,        # e.g. 'deck_exclusive', 'promo', 'gift_set'
        "product_name": None,        # e.g. 'Paradox Rift Trainer Kit'
        "parse_ok":     False,
    }

    name = variation_name.strip()

    # ── Step 0: Detect promo patterns BEFORE standard parsing ────────────────
    for promo_re, promo_set in PROMO_PATTERNS:
        m = promo_re.match(name)
        if m:
            card_num  = m.group(1).lstrip("0") or "0"
            card_name = m.group(2).strip()
            # Strip any trailing promo label from card name
            card_name = re.sub(r'\s+Black\s+Star\s+Promo$', '', card_name, flags=re.IGNORECASE).strip()
            card_name = re.sub(r'\s+Promo$', '', card_name, flags=re.IGNORECASE).strip()
            result.update({
                "card_number":  card_num,
                "set_total":    None,
                "card_name":    card_name,
                "variant_type": "Normal",
                "card_type":    "promo",
                "set_override": promo_set,
                "parse_ok":     True,
            })
            return result

    # ── Step 1: Extract card_number/set_total prefix ──────────────────────────
    num_match = re.match(r"^(\d+)/(\d+)\s+(.+)$", name)
    if not num_match:
        result["card_name"] = name
        return result

    card_num_str  = num_match.group(1).lstrip("0") or "0"
    set_total_str = num_match.group(2)
    remainder     = num_match.group(3).strip()

    result["card_number"] = card_num_str
    result["set_total"]   = set_total_str

# ── Step 2: Strip ALL variant tokens from remainder ──────────────────────
    clean_name    = remainder
    found_variant = None

    # ── Detect source type before stripping ──────────────────────────────────
    if re.search(r'\(Deck\s+Exclusive\)', clean_name, re.IGNORECASE):
        result["source_type"] = "deck_exclusive"
        clean_name = re.sub(r'\s*\(Deck\s+Exclusive\)', '', clean_name, flags=re.IGNORECASE).strip()

    for pattern, variant_label in VARIANT_PATTERNS:
        if re.search(pattern, clean_name, re.IGNORECASE):
            found_variant = variant_label
            break

    # Strip ALL variant-related words in one pass regardless of order
    STRIP_PATTERNS = [
        r"\bReverse\s+H\w*\b",     # "Reverse H", "Reverse Hol", "Reverse Holo", etc.
        r"\bReverse\b",            # standalone "Reverse"
        r"\bHolo\b",               # standalone "Holo"
        r"\bRH\b",                 # abbreviation "RH"
        r"\bCosmos\b",             # "Cosmos" (Cosmos Holo)
        r"\bMaster\s+Ball\b",      # "Master Ball"
        r"\bPoke\s+Ball\b",        # "Poke Ball"
        r"\bPromo\b",              # "Promo"
        r"\s+R$",                  # trailing rarity indicator "R"
        r"\s*\[.*?\]",    # strips anything in brackets e.g. [Ghetsis], [Paldea]
    ]
    for sp in STRIP_PATTERNS:
        clean_name = re.sub(sp, "", clean_name, flags=re.IGNORECASE)

    # Clean up leftover spaces and punctuation
    clean_name = re.sub(r"\s{2,}", " ", clean_name).strip(" -–,")

    result["card_name"]    = clean_name or remainder
    result["variant_type"] = found_variant or "Normal"

    # ── Step 3: Classify card type ────────────────────────────────────────────
    try:
        card_num_int  = int(card_num_str)
        set_total_int = int(set_total_str)
        result["card_type"] = classify_card_type(
            card_num_int, set_total_int, result["variant_type"], result["card_name"]
        )
    except ValueError:
        pass

    result["parse_ok"] = True
    return result


def infer_set_name_from_title(title: str) -> str | None:
    """Best-effort: extract set name from the eBay listing title."""
    if not title:
        return None

    KNOWN_SETS = [
        # Scarlet & Violet
        "Obsidian Flames", "Paldea Evolved", "Paradox Rift",
        "Temporal Forces", "Twilight Masquerade", "Shrouded Fable",
        "Stellar Crown", "Surging Sparks", "Prismatic Evolutions",
        "Journey Together", "Destined Rivals", "Paldean Fates",
        "Black Bolt", "White Flare", "151",
        "Scarlet & Violet", "Scarlet&Violet",

        # Sword & Shield
        "Champion's Path", "Chilling Reign", "Battle Styles",
        "Shining Fates", "Vivid Voltage", "Darkness Ablaze",
        "Evolving Skies", "Fusion Strike", "Brilliant Stars",
        "Astral Radiance", "Lost Origin", "Silver Tempest",
        "Crown Zenith", "Sword & Shield", "Sword&Shield",

        # Mega Evolution
        "Mega Evolution", "Phantasmal Flames", "Ascended Heroes",
        "Perfect Order", "Chaos Rising",

        # Other
        "Pokemon GO", "Pokémon GO", "Pokemon X GO",
    ]

    title_lower = title.lower()
    for set_name in KNOWN_SETS:
        if set_name.lower() in title_lower:
            return set_name

    return None


# ── Quick test ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    test_cases = [
        "153/197 Melmetal ex",
        "190/197 Ortega Reverse Holo RH",
        "198/197 Gloom",
        "044/197 Charmander RH",
        "001/197 Oddish",
        "230/197 Charizard ex Special Illustration Rare",
        "192/197 Pokémon League Headquarters Reverse Holo R",
        "SVP 044 Charmander Black Star Promo",
        "SWSH001 Pikachu",
        "SVP044 Eevee",
    ]
    print(f"{'Raw':<55} {'#':<5} {'Name':<30} {'Variant':<18} {'Type':<12} {'Set Override'}")
    print("-" * 145)
    for t in test_cases:
        r = parse_variation_name(t)
        print(
            f"{r['raw']:<55} "
            f"{r['card_number'] or '?':<5} "
            f"{r['card_name'] or '?':<30} "
            f"{r['variant_type']:<18} "
            f"{r['card_type']:<12} "
            f"{r['set_override'] or '-'}"
        )
