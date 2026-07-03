"""
utils/ebay_parser.py — Parse eBay variation names into structured card data

Your variation names follow these patterns:
    "153/197 Melmetal ex"
    "190/197 Ortega Reverse Holo RH"
    "198/197 Gloom"              ← above base = ultra rare
    "044/197 Charmander RH"
    "SVP 044 Charmander Black Star Promo"   ← promo pattern
    "SWSH001 Pikachu"                        ← SWSH promo pattern

This module extracts the SEVEN-AXIS variant model (lookup codes; NULL = standard):
    card_number   → "153"
    set_total     → "197"
    card_name     → "Melmetal ex"
    foil_type     → 'non_holo' | 'holo' | 'reverse_holo' | None
    foil_pattern  → 'poke_ball' | 'master_ball' | 'love_ball' | ... | None
    texture       → 'cosmos' | 'hd_cosmos' | 'galaxy_cosmos' | None
    material      → 'metal' | None
    size          → 'jumbo' | None
    stamp_type    → '1st_edition' | 'pokemon_center' | 'pokemon_day' | ... | None
    source_type   → 'deck_exclusive' | 'product_exclusive' | 'box_topper' | 'stamp_promo' | None
    is_shiny      → bool (manual axis; parser sets False — user curates)
    set_override  → set name override for promos

NOTE: variant_type and card_type are RETIRED. Pricing/classification is a
separate concern; this parser captures FACTS only.
"""

import re

# ── Suffix tokens that indicate variant/finish ────────────────────────────────
VARIANT_PATTERNS = [
    (r"\bRH\b",                   "Reverse Holo"),
    (r"\bReverse\s+Holo\b",       "Reverse Holo"),
    (r"\bReverse\b",              "Reverse Holo"),
    (r"\bReverse\s+H\b",          "Reverse Holo"),   # truncated "Reverse H"
    (r"\bCosmos\s+Holo\b",        "Cosmos Holo"),
    (r"\bCosmo\b",                "Cosmos Holo"),   # short form of Cosmos Holo
    (r"\bMaster\s+Ball\b",        "Master Ball Pattern"),
    (r"\bPoke\s+Ball\b",          "Poke Ball Pattern"),
    (r"\bHolo\b",                 "Holo"),
    (r"\bCosmo\s+Holo\b",         "Cosmos Holo"),   
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
    (
        re.compile(r'^SVE\s*0*(\d+)\s+(.+?)$', re.IGNORECASE),
        "Scarlet & Violet Energies",
    ),
    (
        re.compile(r'^MEE\s*0*(\d+)\s+(.+?)$', re.IGNORECASE),
        "Mega Evolution Energies",
    ),
    (
        re.compile(r'^MEP\s*0*(\d+)\s+(.+?)$', re.IGNORECASE),
        "Mega Evolution Black Star Promos",
    ),
]

# ── Internal-label → lookup-code normalization ────────────────────────────────
# The detection logic below uses human labels internally (legacy). This maps
# them to the seven-axis lookup CODES the DB expects. Anything not mapped → None
# (standard/none).

def _normalize_axes(result: dict) -> dict:
    """
    Convert the parser's internal working labels into the seven-axis lookup
    codes. Mutates and returns result. Splits the old conflated values:
      - 'Cosmos Holo'  -> foil_type stays holo/reverse_holo, texture='cosmos'
      - 'Metal Card'   -> material='metal'
      - 'Shiny'        -> dropped from foil_pattern (is_shiny is manual)
      - ball patterns  -> foil_pattern code + foil_type='reverse_holo'
    """
    vt  = (result.pop("variant_type", None) or "").strip()
    fp  = (result.get("foil_pattern") or "").strip()

    foil_type   = None
    foil_pattern = None
    texture     = None
    material    = result.get("material")
    size        = result.get("size")

    vt_l = vt.lower()
    fp_l = fp.lower()

    # --- material (Metal Card) ---
    if vt_l == "metal card":
        material = "metal"
        vt_l = ""  # not a foil

    # --- base foil_type from variant_type ---
    if "reverse" in vt_l:
        foil_type = "reverse_holo"
    elif "cosmos" in vt_l or "cosmo" in vt_l:
        # standalone cosmos = a holo whose texture is cosmos
        foil_type = "holo"
        texture   = "cosmos"
    elif "holo" in vt_l:
        foil_type = "holo"
    elif vt_l in ("normal", "", "non-holo", "non_holo"):
        foil_type = "non_holo"
    else:
        foil_type = "non_holo"

    # --- ex / V / VMAX / VSTAR / GX cards are holo by definition ---
    # If no explicit reverse/holo was detected (foil_type came out non_holo),
    # infer holo from the card-name suffix. Reverse holo, if detected, wins.
    if foil_type == "non_holo":
        name_l = (result.get("card_name") or "").lower().strip()
        HOLO_SUFFIXES = (" ex", " v", " vmax", " vstar", " gx",
                         " gx tag team", " vunion", " v-union")
        if any(name_l.endswith(s) for s in HOLO_SUFFIXES):
            foil_type = "holo"

    # --- foil_pattern / texture from the old foil_pattern field ---
    BALL_CODES = {
        "poke ball": "poke_ball", "poke ball pattern": "poke_ball",
        "master ball": "master_ball", "master ball pattern": "master_ball",
        "friend ball": "friend_ball", "love ball": "love_ball",
        "quick ball": "quick_ball", "dusk ball": "dusk_ball",
        "team rocket": "team_rocket", "energy symbol": "energy_symbol",
    }
    if fp_l in ("cosmos holo", "cosmos", "cosmo", "cosmo holo"):
        # reverse holo with cosmos texture (the Eevee case)
        texture = "cosmos"
        if not foil_type or foil_type == "non_holo":
            foil_type = "reverse_holo"
    elif fp_l == "shiny":
        # shiny is a manual card_master axis, not a foil pattern — drop it
        pass
    elif fp_l in BALL_CODES:
        foil_pattern = BALL_CODES[fp_l]
        foil_type = "reverse_holo"   # ball patterns are reverse holos

    result["foil_type"]    = foil_type
    result["foil_pattern"] = foil_pattern
    result["texture"]      = texture
    result["material"]     = material
    result["size"]         = size
    # stamp_type / source_type already use codes from the detection logic
    result.setdefault("stamp_type", None)
    result.setdefault("source_type", None)
    result.setdefault("is_shiny", False)
    return result


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
        "variant_type": "Normal",   # internal working label; normalized away at end
        "set_override": None,
        "foil_pattern": None,        # internal working label; normalized at end
        "texture":      None,
        "material":     None,
        "size":         None,
        "source_type":  None,
        "stamp_type":   None,
        "is_shiny":     False,
        "product_name": None,
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
            card_name = re.sub(r'\s*\(Black\s+Star\s+Promo\)$', '', card_name, flags=re.IGNORECASE).strip()
            card_name = re.sub(r'\s+Black\s+Star\s+Promo$', '', card_name, flags=re.IGNORECASE).strip()
            card_name = re.sub(r'\s+Promo$', '', card_name, flags=re.IGNORECASE).strip()

            # Detect variant BEFORE stripping Cosmo
            promo_variant = "Normal"
            if re.search(r'\bCosmo\s+Holo\b|\bCosmos\s+Holo\b|\bCosmo\b', card_name, re.IGNORECASE):
                promo_variant = "Cosmos Holo"

            # Now strip Cosmo from name
            card_name = re.sub(r'\s+Cosmo\s+Holo$', '', card_name, flags=re.IGNORECASE).strip()
            card_name = re.sub(r'\s+Cosmo$', '', card_name, flags=re.IGNORECASE).strip()

            # Detect plain Holo suffix (e.g. "Basic Grass Energy Holo")
            if promo_variant == "Normal" and re.search(r'\bHolo$', card_name, re.IGNORECASE):
                promo_variant = "Holo"
                card_name = re.sub(r'\s+Holo$', '', card_name, flags=re.IGNORECASE).strip()

            # Detect Pokemon Center Stamp suffix
            if re.search(r'Pok[eé]mon\s+Center\s+Stamp$', card_name, re.IGNORECASE):
                result["stamp_type"] = "pokemon_center"
                card_name = re.sub(r'\s*Pok[eé]mon\s+Center\s+Stamp$', '', card_name, flags=re.IGNORECASE).strip()

            result.update({
                "card_number":  card_num,
                "set_total":    None,
                "card_name":    card_name,
                "variant_type": promo_variant,
                "set_override": promo_set,
                "parse_ok":     True,
            })
            return _normalize_axes(result)

    # ── Step 1: Extract card_number/set_total prefix ──────────────────────────
    num_match = re.match(r"^(\d+)/(\d+)\s+(.+)$", name)
    if not num_match:
        result["card_name"] = name
        return _normalize_axes(result)

    card_num_str  = num_match.group(1).lstrip("0") or "0"
    set_total_str = num_match.group(2)
    remainder     = num_match.group(3).strip()

    result["card_number"] = card_num_str
    result["set_total"]   = set_total_str


    # ── Special-case: "Poke Ball"/"Master Ball" Trainer item cards (the card NAME is the ball type) ──
    BALL_ITEM_NAMES = ["Poke Ball", "Master Ball"]
    for ball_card in BALL_ITEM_NAMES:
        if re.match(r'^' + re.escape(ball_card) + r'\b', remainder, re.IGNORECASE):
            rest = remainder[len(ball_card):].strip()
            result["card_name"] = ball_card
            result["variant_type"] = "Reverse Holo" if re.search(r'\bRH\b|\bReverse\b', rest, re.IGNORECASE) else "Normal"
            result["parse_ok"] = True
            return _normalize_axes(result)

    # ── Step 2: Strip ALL variant tokens from remainder ──────────────────────
    clean_name    = remainder
    found_variant = None

    # ── Detect Shiny variant ──────────────────────────────────────────────────
    if re.search(r'\bShiny\b', clean_name, re.IGNORECASE):
        result["foil_pattern"] = "Shiny"
        clean_name = re.sub(r'\s+Shiny\b', '', clean_name, flags=re.IGNORECASE).strip()

    # ── Detect Reverse Cosmos Holo combo ───────────────────────────────────────
    if re.search(r'\bCosmos?\b', clean_name, re.IGNORECASE) and re.search(r'\bReverse\b|\bRH\b', clean_name, re.IGNORECASE):
        result["foil_pattern"] = "Cosmos Holo"
        result["variant_type"] = "Reverse Holo"
        clean_name = re.sub(r'\bCosmos?\s+Holo\b|\bCosmos?\b', '', clean_name, flags=re.IGNORECASE).strip()
        clean_name = re.sub(r'\bReverse\s+Holo\b|\bReverse\b|\bRH\b', '', clean_name, flags=re.IGNORECASE).strip()
        clean_name = re.sub(r"\s{2,}", " ", clean_name).strip()

    # ── Detect Ball-pattern Reverse Holo variants (both orderings) ────────────
    BALL_PATTERN_TYPES = ["Friend Ball", "Love Ball", "Quick Ball", "Dusk Ball", "Team Rocket", "Poke Ball", "Pokeball", "Master Ball"]
    BALL_TYPE_NORMALIZE = {"pokeball": "Poke Ball"}

    ball_alt = '|'.join(re.escape(b) for b in BALL_PATTERN_TYPES)
    ball_pattern_re = re.compile(
        r'(?:\bRH\s+(' + ball_alt + r')\b|\b(' + ball_alt + r')\s+RH\b).*$',
        re.IGNORECASE
    )
    ball_match = ball_pattern_re.search(clean_name)
    if ball_match:
        matched_ball = ball_match.group(1) or ball_match.group(2)
        normalized = BALL_TYPE_NORMALIZE.get(matched_ball.lower())
        if normalized:
            matched_ball = normalized
        else:
            for b in BALL_PATTERN_TYPES:
                if b.lower() == matched_ball.lower():
                    matched_ball = b
                    break
        result["foil_pattern"] = matched_ball
        result["variant_type"] = "Reverse Holo"
        clean_name = ball_pattern_re.sub('', clean_name).strip()

    # ── Strip trailing "Energy" after RH/Reverse Holo (e.g. "Pikachu RH Energy") ──
    clean_name = re.sub(r'\b(RH|Reverse(?:\s+Holo)?)\s+Energy\b', r'\1', clean_name, flags=re.IGNORECASE)       

    # ── Detect stamp type FIRST (before removing source markers) ──────────────
    if re.search(r'\b1st\s+Edition\b|\b1st\s+Ed\b', clean_name, re.IGNORECASE):
        result["stamp_type"] = "1st_edition"
        clean_name = re.sub(r'\s*\b1st\s+Edition\b|\s*\b1st\s+Ed\b', '', clean_name, flags=re.IGNORECASE).strip()
    elif re.search(r'Pok[eé]mon\s+Center\s+Stamp', clean_name, re.IGNORECASE):
        result["stamp_type"] = "pokemon_center"
        clean_name = re.sub(r'\s*Pok[eé]mon\s+Center\s+Stamp', '', clean_name, flags=re.IGNORECASE).strip()
    elif re.search(r'\bPrerelease\b', clean_name, re.IGNORECASE):
        result["stamp_type"] = "prerelease"
        clean_name = re.sub(r'\s*\bPrerelease\b', '', clean_name, flags=re.IGNORECASE).strip()
    elif re.search(r'\bMega\s+Evolution\s+Stamp\b', clean_name, re.IGNORECASE):
        result["stamp_type"] = "mega_evolution"
        clean_name = re.sub(r'\s*\bMega\s+Evolution\s+Stamp\b', '', clean_name, flags=re.IGNORECASE).strip()
    elif re.search(r'Pokemon\s+Day|Pok[eé]mon\s+Day', clean_name, re.IGNORECASE):
        result["stamp_type"] = "pokemon_day"
        result["source_type"] = "stamp_promo"
        clean_name = re.sub(r'\s*Pok[eé]mon\s+Day\s*\d*', '', clean_name, flags=re.IGNORECASE).strip()
    elif re.search(r'\bPrismatic\s+Evolutions?\s+Stamp\b', clean_name, re.IGNORECASE):
        result["stamp_type"] = "prismatic_evolution"
        result["source_type"] = "stamp_promo"
        clean_name = re.sub(r'\s*\bPrismatic\s+Evolutions?\s+Stamp\b', '', clean_name, flags=re.IGNORECASE).strip()

    # ── Detect Box Topper independently (can combine with other stamps) ───────
    if re.search(r'\bBox\s+Topper\b', clean_name, re.IGNORECASE):
        result["source_type"] = "box_topper"
        clean_name = re.sub(r'\s*\bBox\s+Topper\b', '', clean_name, flags=re.IGNORECASE).strip()

    # ── Detect source type ────────────────────────────────────────────────────
    if re.search(r'\(?Deck\s+Exclusive\)?', clean_name, re.IGNORECASE):
        result["source_type"] = "deck_exclusive"
        clean_name = re.sub(r'\s*\(?Deck\s+Exclusive\)?', '', clean_name, flags=re.IGNORECASE).strip()
    elif re.search(r'\(?Product\s+Exclusive\)?', clean_name, re.IGNORECASE):
        result["source_type"] = "product_exclusive"
        clean_name = re.sub(r'\s*\(?Product\s+Exclusive\)?', '', clean_name, flags=re.IGNORECASE).strip()
    elif re.search(r'\bStamp\b', clean_name, re.IGNORECASE):
        result["source_type"] = "stamp_promo"
        clean_name = re.sub(r'\s*\bStamp\b', '', clean_name, flags=re.IGNORECASE).strip()
    elif re.search(r'\(?\s*Metal\s+Card\s*(?:Promo)?\s*\)?', clean_name, re.IGNORECASE):
        result["variant_type"] = "Metal Card"
        clean_name = re.sub(r'\s*\(?\s*Metal\s+Card\s*(?:Promo)?\s*\)?', '', clean_name, flags=re.IGNORECASE).strip()
    elif re.search(r'\bExclusive\b', clean_name, re.IGNORECASE):
        result["source_type"] = "product_exclusive"
        clean_name = re.sub(r'\s*\bExclusive\b', '', clean_name, flags=re.IGNORECASE).strip()
    for pattern, variant_label in VARIANT_PATTERNS:
        if re.search(pattern, clean_name, re.IGNORECASE):
            found_variant = variant_label
            break

    # Strip ALL variant-related words in one pass regardless of order
    STRIP_PATTERNS = [
        # Rarity phrases — these appear in eBay titles but are NOT part of the
        # card name (rarity comes from the API / card_master). Strip longest
        # first so "Special Illustration Rare" goes before "Illustration Rare".
        r"\bSpecial\s+Illustration\s+Rare\b",
        r"\bIllustration\s+Rare\b",
        r"\bHyper\s+Rare\b",
        r"\bUltra\s+Rare\b",
        r"\bDouble\s+Rare\b",
        r"\bSecret\s+Rare\b",
        r"\bRainbow\s+Rare\b",
        r"\bGold(?:en)?\s+(?:Secret\s+)?Rare\b",
        r"\bACE\s+SPEC\b",
        r"\bAmazing\s+Rare\b",
        r"\bRadiant\b",
        r"\bFull\s+Art\b",
        r"\bAlt(?:ernate)?\s+Art\b",
        r"\bSIR\b", r"\bIR\b", r"\bFA\b", r"\bAA\b",   # common abbreviations
        # Variant/finish tokens
        r"\bReverse\s+H\w*\b",     # "Reverse H", "Reverse Hol", "Reverse Holo", etc.
        r"\bReverse\b",            # standalone "Reverse"
        r"\bHolo\b",               # standalone "Holo"
        r"\bRH\b",                 # abbreviation "RH"
        r"\bCosmos\b",             # "Cosmos" (Cosmos Holo)
        r"\bCosmo\b",
        r"\bStamp\b",              # Stamp promo indicator
        r"\bMaster\s+Ball\b",      # "Master Ball"
        r"\bPoke\s+Ball\b",        # "Poke Ball"
        r"\bPromo\b",              # "Promo"
        r"\s+R$",                  # trailing rarity indicator "R"
        r"\s*\[.*?\]",             # strips anything in brackets e.g. [Ghetsis], [Paldea]
        r"\s*\[[^\]]*$",           # dangling unclosed bracket (truncated, e.g. "[Sycamore")
        r"\s*\([^)]*\)\s*$",       # strips trailing (content) e.g. (Sada), (Turo)
    ]
    for sp in STRIP_PATTERNS:
        clean_name = re.sub(sp, "", clean_name, flags=re.IGNORECASE)

    # Clean up leftover spaces and punctuation
    clean_name = re.sub(r"\s{2,}", " ", clean_name).strip(" -–,")

    # ── Re-check holo suffix AFTER cleaning (e.g. "Charizard ex Special
    #    Illustration Rare" -> "Charizard ex" now ends in " ex") ──
    result["card_name"] = clean_name or remainder
    _name_l = (result["card_name"] or "").lower().strip()
    _HOLO_SUFFIXES = (" ex", " v", " vmax", " vstar", " gx",
                      " gx tag team", " vunion", " v-union")
    if (result["variant_type"] in ("Normal", "")
            and any(_name_l.endswith(s) for s in _HOLO_SUFFIXES)):
        # mark as holo so _normalize_axes picks it up
        result["variant_type"] = "Holo"
    # Only set variant_type if not already set (e.g. Metal Card)
    if result["variant_type"] == "Normal":
        result["variant_type"] = found_variant or "Normal"

    result["parse_ok"] = True
    return _normalize_axes(result)


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
        "025/165 Pikachu RH Master Ball",
        "025/165 Pikachu RH Poke Ball",
        "036/165 Eevee Reverse Holo Cosmos Pokemon Day",
        "SVP 050 Pikachu Cosmo Holo",
        "001/091 Charizard Metal Card Promo",
    ]
    cols = ["#", "Name", "foil_type", "foil_pattern", "texture",
            "material", "size", "stamp_type", "source_type"]
    print(f"{'Raw':<48} " + " ".join(f"{c:<13}" for c in cols))
    print("-" * 170)
    for t in test_cases:
        r = parse_variation_name(t)
        print(
            f"{r['raw']:<48} "
            f"{(r['card_number'] or '?'):<13} "
            f"{(r['card_name'] or '?'):<13.13} "
            f"{str(r['foil_type']):<13} "
            f"{str(r['foil_pattern']):<13} "
            f"{str(r['texture']):<13} "
            f"{str(r['material']):<13} "
            f"{str(r['size']):<13} "
            f"{str(r['stamp_type']):<13} "
            f"{str(r['source_type']):<13}"
        )
