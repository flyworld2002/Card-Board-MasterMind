"""
utils/set_name_map.py
Maps TCGPlayer set labels to PokemonTCG API set IDs.

TCGPlayer uses labels like:
  "SV: Black Bolt"          → API set ID: "zsv10pt5"
  "SV08: Surging Sparks"    → API set ID: "sv8"
  "ME02: Phantasmal Flames" → API set ID: "me2"

Using set IDs is more reliable than set names — no em dashes,
no spacing issues, no capitalization mismatches.

Strategy:
  1. Strip the TCGPlayer prefix (SV:, SV08:, ME:, ME02:, etc.)
  2. Look up cleaned name in TCGPLAYER_TO_ID
  3. Return set ID for use in API query: set.id:sv8
"""

import re

# TCGPlayer display name (after prefix stripped) → API set ID
TCGPLAYER_TO_ID = {
    # ── Mega Evolution (ME prefix) ──────────────────────────────
    "Mega Evolution":            "me1",
    "Phantasmal Flames":         "me2",
    "Ascended Heroes":           "me2pt5",
    "Perfect Order":             "me3",
    "Chaos Rising":              "me4",

    # ── Scarlet & Violet (SV prefix) ────────────────────────────
    "Scarlet & Violet":          "sv1",
    "Scarlet&Violet":            "sv1",
    "Scarlet & Violet Base Set": "sv1",
    "Paldea Evolved":            "sv2",
    "Obsidian Flames":           "sv3",
    "151":                       "sv3pt5",
    "Scarlet & Violet 151":      "sv3pt5",
    "Paradox Rift":              "sv4",
    "Paldean Fates":             "sv4pt5",
    "Temporal Forces":           "sv5",
    "Twilight Masquerade":       "sv6",
    "Shrouded Fable":            "sv6pt5",
    "Stellar Crown":             "sv7",
    "Surging Sparks":            "sv8",
    "Prismatic Evolutions":      "sv8pt5",
    "Journey Together":          "sv9",
    "Destined Rivals":           "sv10",
    "Black Bolt":                "zsv10pt5",
    "White Flare":               "rsv10pt5",
    "Scarlet & Violet Energies": "sve",

    # ── SV Promos ───────────────────────────────────────────────
    "Scarlet & Violet Promo Cards":         "svp",
    "Scarlet & Violet Promos":              "svp",
    "Scarlet & Violet Black Star Promos":   "svp",

    # ── Sword & Shield (SWSH prefix) ────────────────────────────
    "Sword & Shield":            "swsh1",
    "Sword&Shield":              "swsh1",
    "Sword & Shield Base Set":   "swsh1",
    "Rebel Clash":               "swsh2",
    "Darkness Ablaze":           "swsh3",
    "Champion's Path":           "swsh35",
    "Vivid Voltage":             "swsh4",
    "Shining Fates":             "swsh45",
    "Battle Styles":             "swsh5",
    "Chilling Reign":            "swsh6",
    "Evolving Skies":            "swsh7",
    "Fusion Strike":             "swsh8",
    "Brilliant Stars":           "swsh9",
    "Astral Radiance":           "swsh10",
    "Lost Origin":               "swsh11",
    "Silver Tempest":            "swsh12",
    "Crown Zenith":              "swsh12pt5",
    "Crown Zenith: Galarian Gallery": "swsh12pt5gg",
    "Crown Zenith Galarian Gallery":  "swsh12pt5gg",
    "Celebrations":              "cel25",
    "SWSH Black Star Promos":    "swshp",
    "Sword & Shield Promo Cards": "swshp",
    "Sword & Shield Black Star Promos": "swshp",
    "Pokemon GO":                "pgo",
    "Pokémon GO":                "pgo",
    "Pokemon X GO":              "pgo",
    "McDonald's 2023":           "mcd23",
    "McDonald's Collection 2023": "mcd23",

    # ── Sun & Moon (SM prefix) ──────────────────────────────────
    "Sun & Moon":                "sm1",
    "Guardians Rising":          "sm2",
    "Burning Shadows":           "sm3",
    "Shining Legends":           "sm35",
    "Crimson Invasion":          "sm4",
    "Ultra Prism":               "sm5",
    "Forbidden Light":           "sm6",
    "Celestial Storm":           "sm7",
    "Dragon Majesty":            "sm75",
    "Lost Thunder":              "sm8",
    "Team Up":                   "sm9",
    "Unbroken Bonds":            "sm10",
    "Unified Minds":             "sm11",
    "Hidden Fates":              "sm115",
    "Cosmic Eclipse":            "sm12",
    "SM Black Star Promos":      "smp",
    "Sun & Moon Promo Cards":    "smp",

    # ── XY ──────────────────────────────────────────────────────
    "XY":                        "xy1",
    "Flashfire":                 "xy2",
    "Furious Fists":             "xy3",
    "Phantom Forces":            "xy4",
    "Primal Clash":              "xy5",
    "Roaring Skies":             "xy6",
    "Ancient Origins":           "xy7",
    "BREAKthrough":              "xy8",
    "BREAKpoint":                "xy9",
    "Fates Collide":             "xy10",
    "Steam Siege":               "xy11",
    "Evolutions":                "xy12",
    "Generations":               "g1",
    "XY Black Star Promos":      "xyp",
    "XY Promo Cards":            "xyp",

    # ── Black & White ────────────────────────────────────────────
    "Black & White":             "bw1",
    "Emerging Powers":           "bw2",
    "Noble Victories":           "bw3",
    "Next Destinies":            "bw4",
    "Dark Explorers":            "bw5",
    "Dragons Exalted":           "bw6",
    "Boundaries Crossed":        "bw7",
    "Plasma Storm":              "bw8",
    "Plasma Freeze":             "bw9",
    "Plasma Blast":              "bw10",
    "Legendary Treasures":       "bw11",
    "BW Black Star Promos":      "bwp",
    "Black & White Promo Cards": "bwp",

    # ── HeartGold & SoulSilver ───────────────────────────────────
    "HeartGold & SoulSilver":    "hgss1",
    "HS—Unleashed":              "hgss2",
    "HS—Undaunted":              "hgss3",
    "HS—Triumphant":             "hgss4",
    "Call of Legends":           "col1",
    "HGSS Black Star Promos":    "hsp",

    # ── Diamond & Pearl ─────────────────────────────────────────
    "Diamond & Pearl":           "dp1",
    "Mysterious Treasures":      "dp2",
    "Secret Wonders":            "dp3",
    "Great Encounters":          "dp4",
    "Majestic Dawn":             "dp5",
    "Legends Awakened":          "dp6",
    "Stormfront":                "dp7",
    "DP Black Star Promos":      "dpp",

    # ── Platinum ─────────────────────────────────────────────────
    "Platinum":                  "pl1",
    "Rising Rivals":             "pl2",
    "Supreme Victors":           "pl3",
    "Arceus":                    "pl4",

    # ── EX Series ────────────────────────────────────────────────
    "Ruby & Sapphire":           "ex1",
    "Sandstorm":                 "ex2",
    "Dragon":                    "ex3",
    "Team Magma vs Team Aqua":   "ex4",
    "Hidden Legends":            "ex5",
    "FireRed & LeafGreen":       "ex6",
    "Team Rocket Returns":       "ex7",
    "Deoxys":                    "ex8",
    "Emerald":                   "ex9",
    "Unseen Forces":             "ex10",
    "Delta Species":             "ex11",
    "Legend Maker":              "ex12",
    "Holon Phantoms":            "ex13",
    "Crystal Guardians":         "ex14",
    "Dragon Frontiers":          "ex15",
    "Power Keepers":             "ex16",

    # ── Neo ──────────────────────────────────────────────────────
    "Neo Genesis":               "neo1",
    "Neo Discovery":             "neo2",
    "Neo Revelation":            "neo3",
    "Neo Destiny":               "neo4",

    # ── Gym ──────────────────────────────────────────────────────
    "Gym Heroes":                "gym1",
    "Gym Challenge":             "gym2",

    # ── Base ─────────────────────────────────────────────────────
    "Base Set":                  "base1",
    "Base":                      "base1",
    "Jungle":                    "base2",
    "Fossil":                    "base3",
    "Base Set 2":                "base4",
    "Team Rocket":               "base5",
    "Legendary Collection":      "base6",
    "Wizards Black Star Promos": "basep",

    # ── E-Card ───────────────────────────────────────────────────
    "Expedition Base Set":       "ecard1",
    "Aquapolis":                 "ecard2",
    "Skyridge":                  "ecard3",
}


def get_set_id(tcgplayer_set: str) -> str | None:
    """
    Convert TCGPlayer set label to API set ID.

    Examples:
        "SV: Black Bolt"          → "zsv10pt5"
        "SV08: Surging Sparks"    → "sv8"
        "ME02: Phantasmal Flames" → "me2"
        "SV: Scarlet & Violet 151" → "sv3pt5"
        "SWSH: Crown Zenith: Galarian Gallery" → "swsh12pt5gg"

    Returns None if not found (caller should fall back to name search).
    """
    if not tcgplayer_set:
        return None

    # Strip TCGPlayer prefix: "SV08:", "ME02:", "SV:", "SWSH:", "SVE:", etc.
    cleaned = re.sub(r'^[A-Z]+\d*(?:pt\d+)?:\s*', '', tcgplayer_set).strip()

    # Direct lookup
    set_id = TCGPLAYER_TO_ID.get(cleaned)
    if set_id:
        return set_id

    # Try stripping another prefix layer (e.g. "SWSH: Crown Zenith: Galarian Gallery")
    cleaned2 = re.sub(r'^[A-Z][^:]+:\s*', '', cleaned).strip()
    if cleaned2 != cleaned:
        set_id = TCGPLAYER_TO_ID.get(cleaned2)
        if set_id:
            return set_id

    return None


# Keep clean_set_name for backwards compatibility
def clean_set_name(tcgplayer_set: str) -> str:
    """Legacy function — returns set name for display purposes."""
    if not tcgplayer_set:
        return ""
    cleaned = re.sub(r'^[A-Z]+\d*:\s*', '', tcgplayer_set).strip()
    return TCGPLAYER_TO_ID.get(cleaned, cleaned)
