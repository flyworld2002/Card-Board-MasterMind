"""
utils/set_name_map.py
Maps TCGPlayer set labels to either the PokemonTCG API's set ID or our own
card_sets row, backed by the tcgplayer_set_aliases table (not a hardcoded
dict -- a new/renamed set just needs a row inserted in Supabase, no code
deploy or service restart).

TCGPlayer uses labels like:
  "SV: Black Bolt"          -> API set ID: "zsv10pt5"
  "SV08: Surging Sparks"    -> API set ID: "sv8"
  "ME02: Phantasmal Flames" -> API set ID: "me2"

Some labels (promo sets the PokemonTCG API doesn't index at all, e.g.
"ME: Mega Evolution Promo") have no API set ID -- those rows carry our own
card_sets.id (set_id) instead. A row can carry either or both; see
get_set_alias().

Strategy:
  1. Strip the TCGPlayer prefix (SV:, SV08:, ME:, ME02:, etc.)
  2. Look up the cleaned label in tcgplayer_set_aliases
  3. Use api_set_id for a live API query (set.id:sv8) and/or set_id for a
     direct local card_sets match
"""

import re


def _strip_tcgplayer_prefix(tcgplayer_set: str) -> str:
    """Strip TCGPlayer's prefix: "SV08:", "ME02:", "SV:", "SWSH:", "SVE:", etc."""
    return re.sub(r'^[A-Z]+\d*(?:pt\d+)?:\s*', '', tcgplayer_set).strip()


def get_set_alias(tcgplayer_set: str) -> dict | None:
    """
    Look up a TCGPlayer set label in tcgplayer_set_aliases.

    Returns {"set_id": ..., "api_set_id": ...} (either may be None) or
    None if this label has no alias row at all.

    Examples:
        "SV: Black Bolt"          -> {"set_id": None, "api_set_id": "zsv10pt5"}
        "ME: Mega Evolution Promo" -> {"set_id": <uuid>, "api_set_id": None}
        "SWSH: Crown Zenith: Galarian Gallery" -> {"set_id": None, "api_set_id": "swsh12pt5gg"}
    """
    if not tcgplayer_set:
        return None

    from db.connection import find_tcgplayer_set_alias

    cleaned = _strip_tcgplayer_prefix(tcgplayer_set)
    alias = find_tcgplayer_set_alias(cleaned)
    if alias:
        return alias

    # Try stripping another prefix layer (e.g. "SWSH: Crown Zenith: Galarian Gallery")
    cleaned2 = re.sub(r'^[A-Z][^:]+:\s*', '', cleaned).strip()
    if cleaned2 != cleaned:
        alias = find_tcgplayer_set_alias(cleaned2)
        if alias:
            return alias

    return None


def get_set_id(tcgplayer_set: str) -> str | None:
    """
    Convert TCGPlayer set label to API set ID.

    Examples:
        "SV: Black Bolt"          -> "zsv10pt5"
        "SV08: Surging Sparks"    -> "sv8"
        "ME02: Phantasmal Flames" -> "me2"
        "SV: Scarlet & Violet 151" -> "sv3pt5"

    Returns None if not found (caller should fall back to name search) --
    including for a label whose alias row only has set_id (a promo set the
    API doesn't index), since there's no API set ID to give.
    """
    alias = get_set_alias(tcgplayer_set)
    return alias.get("api_set_id") if alias else None
