"""
utils/ebay_parser.py — Parse eBay variation names into structured card data

Your variation names follow this pattern:
    "153/197 Melmetal ex"
    "190/197 Ortega Reverse Holo RH"
    "198/197 Gloom"          ← above base = ultra rare / secret rare
    "044/197 Charmander RH"

This module extracts:
    card_number   → "153"
    set_total     → "197"
    card_name     → "Melmetal ex"
    variant_type  → "Reverse Holo" | "Normal" | etc.
    card_type     → "common" | "reverse_holo" | "holo" | "ultra_rare"
                    (maps to your price_tiers table)
"""

import re

# ── Suffix tokens that indicate variant/finish ────────────────────────────────
# Order matters: check most specific first
VARIANT_PATTERNS = [
    # Abbreviations embedded in name
    (r"\bRH\b",                   "Reverse Holo"),
    (r"\bReverse\s+Holo\b",       "Reverse Holo"),
    (r"\bReverse\b",              "Reverse Holo"),

    # Special patterns
    (r"\bCosmos\s+Holo\b",        "Cosmos Holo"),
    (r"\bMaster\s+Ball\b",        "Master Ball Pattern"),
    (r"\bPoke\s+Ball\b",          "Poke Ball Pattern"),

    # Holo
    (r"\bHolo\b",                 "Holo"),
]

# ── Card-type classifier (maps to price_tiers.card_type) ─────────────────────
def classify_card_type(card_number: int, set_total: int, variant_type: str) -> str:
    """
    Determine price_tiers card_type based on card number and variant.

    Rules that match your eBay listing structure:
    - Above set_total → ultra_rare (SIR, hyper rare, special illustration)
    - Reverse Holo    → reverse_holo
    - Holo / ex / V   → holo
    - Everything else → common
    """
    if card_number > set_total:
        return "ultra_rare"

    vt = (variant_type or "").lower()
    if "reverse" in vt:
        return "reverse_holo"
    if "holo" in vt or "cosmos" in vt:
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
        "parse_ok":     False,
    }

    name = variation_name.strip()

    # ── Step 1: Extract card_number/set_total prefix ──────────────────────────
    # Matches: "153/197", "044/197", "198/197"
    num_match = re.match(r"^(\d+)/(\d+)\s+(.+)$", name)
    if not num_match:
        result["card_name"] = name  # fallback: use whole string
        return result

    card_num_str  = num_match.group(1).lstrip("0") or "0"
    set_total_str = num_match.group(2)
    remainder     = num_match.group(3).strip()

    result["card_number"] = card_num_str
    result["set_total"]   = set_total_str

    # ── Step 2: Strip variant tokens from remainder to get clean card name ────
    clean_name    = remainder
    found_variant = None

    for pattern, variant_label in VARIANT_PATTERNS:
        if re.search(pattern, clean_name, re.IGNORECASE):
            # Remove the matched token from the name
            clean_name = re.sub(pattern, "", clean_name, flags=re.IGNORECASE).strip()
            # Clean up any trailing punctuation/spaces left behind
            clean_name = re.sub(r"\s{2,}", " ", clean_name).strip(" -–,")
            found_variant = variant_label
            break  # take first (most specific) match

    result["card_name"]    = clean_name or remainder
    result["variant_type"] = found_variant or "Normal"

    # ── Step 3: Classify card type ────────────────────────────────────────────
    try:
        card_num_int  = int(card_num_str)
        set_total_int = int(set_total_str)
        result["card_type"] = classify_card_type(
            card_num_int, set_total_int, result["variant_type"]
        )
    except ValueError:
        pass

    result["parse_ok"] = True
    return result


def infer_set_name_from_title(title: str) -> str | None:
    """
    Best-effort: extract set name from the eBay listing title.

    Examples:
        "Pokemon TCG Obsidian Flames Set All Cards 1-197 YOU CHOOSE"
            → "Obsidian Flames"
        "Pokemon SV3 Obsidian Flames Reverse Holo - Ultra Rare..."
            → "Obsidian Flames"
        "Pokemon TCG Paldea Evolved Commons 1-193 YOU CHOOSE"
            → "Paldea Evolved"
    """
    if not title:
        return None

    # Known set names (extend as you add more sets)
    KNOWN_SETS = [
        "Obsidian Flames",
        "Paldea Evolved",
        "Scarlet & Violet Base",
        "Paradox Rift",
        "Temporal Forces",
        "Twilight Masquerade",
        "Shrouded Fable",
        "Stellar Crown",
        "Surging Sparks",
        "Prismatic Evolutions",
        "Journey Together",
        "Destined Rivals",
        "151",
        "Crown Zenith",
        "Silver Tempest",
        "Lost Origin",
        "Astral Radiance",
        "Brilliant Stars",
        "Fusion Strike",
        "Evolving Skies",
        "Chilling Reign",
        "Battle Styles",
        "Shining Fates",
        "Vivid Voltage",
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
    ]
    print(f"{'Raw':<55} {'#':<5} {'Name':<30} {'Variant':<18} {'Type'}")
    print("-" * 130)
    for t in test_cases:
        r = parse_variation_name(t)
        print(
            f"{r['raw']:<55} "
            f"{r['card_number'] or '?':<5} "
            f"{r['card_name'] or '?':<30} "
            f"{r['variant_type']:<18} "
            f"{r['card_type']}"
        )
