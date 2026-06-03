"""
utils/pokemon_api.py
Looks up card data from the free PokemonTCG API (pokemontcg.io).
No API key required for basic usage (rate-limited to 1000 req/day).
Set POKEMONTCG_API_KEY in .env for higher limits.
"""

import os
import time
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from functools import lru_cache


def _session():
    """Requests session with automatic retry on timeout/connection errors."""
    s = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=2,        # wait 2s, 4s, 8s between retries
        status_forcelist=[429, 500, 502, 503, 504],
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s

BASE_URL = "https://api.pokemontcg.io/v2"


def _headers() -> dict:
    key = os.getenv("POKEMONTCG_API_KEY")
    return {"X-Api-Key": key} if key else {}


# ----------------------------------------------------------------
# Set lookup
# ----------------------------------------------------------------

@lru_cache(maxsize=256)
def get_set_by_name(name: str) -> dict | None:
    """Find a set by name. Returns first match or None."""
    resp = _session().get(
        f"{BASE_URL}/sets",
        params={"q": f'name:"{search_name}"'},
        headers=_headers(),
        timeout=30
    )
    resp.raise_for_status()
    data = resp.json().get("data", [])
    return data[0] if data else None


@lru_cache(maxsize=256)
def get_set_by_code(code: str) -> dict | None:
    """Find a set by its code (e.g. 'base1', 'sv1')."""
    resp = _session().get(
        f"{BASE_URL}/sets/{code}",
        headers=_headers(),
        timeout=30
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json().get("data")


# ----------------------------------------------------------------
# Card lookup
# ----------------------------------------------------------------

def search_cards(name: str, set_name: str = None, set_code: str = None,
                 card_number: str = None, variant: str = None) -> list[dict]:
    """Search using set IDs for precise matching."""
    from utils.set_name_map import get_set_id

    # Nidoran special handling
    NIDORAN_MAP = {
        "nidoran f": ("Nidoran", "\u2640"),
        "nidoran m": ("Nidoran", "\u2642"),
        "nidoran-f": ("Nidoran", "\u2640"),
        "nidoran-m": ("Nidoran", "\u2642"),
    }
    nidoran_filter = None
    if name.lower() in NIDORAN_MAP:
        search_name, nidoran_filter = NIDORAN_MAP[name.lower()]
    else:
        search_name = name

    # Accent map
    ACCENT_MAP = {
        "poke ball":    "Pok\u00e9 Ball",
        "poke pad":     "Pok\u00e9 Pad",
        "poke stop":    "Pok\u00e9 Stop",
        "pokeball":     "Pok\u00e9 Ball",
    }
    search_name = ACCENT_MAP.get(search_name.lower(), search_name)

    # Get API set ID
    api_set_id = get_set_id(set_name) if set_name else None

    # SVE energy: infer number from variant
    SVE_ENERGY_BASE = {
        "basic grass energy": 1, "basic fire energy": 2,
        "basic water energy": 3, "basic lightning energy": 4,
        "basic psychic energy": 5, "basic fighting energy": 6,
        "basic darkness energy": 7, "basic metal energy": 8,
    }
    SVE_VARIANT_OFFSET = {"cosmos holo": 0, "cracked ice holo": 8, "holo": 0}
    if "energy" in name.lower() and not card_number and api_set_id == "sve":
        base = SVE_ENERGY_BASE.get(name.lower())
        if base is not None:
            offset = SVE_VARIANT_OFFSET.get((variant or "").lower(), 0)
            card_number = str(base + offset).zfill(3)

    # Clean card number
    clean_number = card_number.split("/")[0].strip() if card_number else None

    # Number variants: original + stripped leading zeros
    numbers_to_try = []
    if clean_number:
        numbers_to_try.append(clean_number)
        stripped = clean_number.lstrip("0") or clean_number
        if stripped != clean_number:
            numbers_to_try.append(stripped)

    # Pass 1: name + number + set ID (most precise)
    for num in numbers_to_try:
        if api_set_id:
            results = _api_search(f'name:"{search_name}" number:{num} set.id:{api_set_id}')
            if results:
                return results

    # Pass 2: name + number — filter by set ID
    for num in numbers_to_try:
        results = _api_search(f'name:"{search_name}" number:{num}')
        if not results:
            continue
        if api_set_id:
            filtered = [c for c in results if c["set"]["id"] == api_set_id]
            if filtered:
                return filtered
            continue  # Don't return wrong set
        if len(results) == 1:
            return results

    # Pass 3: name + set ID (no number — take lowest numbered = base card)
    if api_set_id:
        results = _api_search(f'name:"{search_name}" set.id:{api_set_id}')
        if results:
            if nidoran_filter:
                filtered = [c for c in results if nidoran_filter in c["name"]]
                if filtered:
                    return [filtered[0]]
            return [_sort_by_number(results)[0]]

    # Pass 4: name only — must match set ID
    results = _api_search(f'name:"{search_name}"')
    if results and api_set_id:
        filtered = [c for c in results if c["set"]["id"] == api_set_id]
        if filtered:
            return [_sort_by_number(filtered)[0]]
        return []

    if results:
        return [_sort_by_number(results)[0]]

    return []


def _normalize_name(name: str) -> str:
    """Normalize card name for comparison - handle special chars."""
    return (name.lower()
            .replace("♀", " f").replace("♂", " m")
            .replace("-", " ").strip())


def _sort_by_number(cards: list) -> list:
    """Sort cards by number numerically (handles non-numeric like 'SWSH001')."""
    def num_key(c):
        n = c.get("number", "9999")
        # Extract leading digits for sorting
        import re
        m = re.match(r'(\d+)', str(n))
        return int(m.group(1)) if m else 9999
    return sorted(cards, key=num_key)


def _filter_by_variant(results: list, variant: str) -> list:
    """
    Filter API results by variant string.
    Handles variants like "Master Ball Pattern", "Reverse Holo", "1st Edition" etc.
    The API stores these in subtypes or the card name itself.
    """
    variant_lower = variant.lower()

    # Check subtypes, name, and set name for variant keywords
    filtered = []
    for c in results:
        subtypes = " ".join(c.get("subtypes", [])).lower()
        card_name_lower = c.get("name", "").lower()
        # Master Ball Pattern cards typically have it in their name
        if variant_lower in card_name_lower:
            filtered.append(c)
        elif variant_lower in subtypes:
            filtered.append(c)
        # Check if variant keywords appear in set name (e.g. promos)
        elif variant_lower in c.get("set", {}).get("name", "").lower():
            filtered.append(c)

    return filtered


def _api_search(q: str, page_size: int = 20) -> list[dict]:
    """Raw API search helper."""
    resp = _session().get(
        f"{BASE_URL}/cards",
        params={"q": q, "pageSize": page_size},
        headers=_headers(),
        timeout=30
    )
    resp.raise_for_status()
    return resp.json().get("data", [])


def get_card_by_id(card_id: str) -> dict | None:
    """Fetch a specific card by its API ID (e.g. 'base1-4')."""
    resp = _session().get(
        f"{BASE_URL}/cards/{card_id}",
        headers=_headers(),
        timeout=30
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json().get("data")


# ----------------------------------------------------------------
# Parsing helpers
# ----------------------------------------------------------------

def parse_card_master_fields(api_card: dict) -> dict:
    """Extract card_master fields from a PokemonTCG API card object."""
    subtypes = api_card.get("subtypes", [])
    variant  = _detect_variant(api_card)
    finish   = _detect_finish(api_card)

    return {
        "external_id":      api_card["id"],
        "name":             api_card["name"],
        "card_number":      api_card.get("number", ""),
        "rarity":           api_card.get("rarity"),
        "variant":          variant,
        "finish":           finish,
        "is_promo":         "Promo" in (api_card.get("rarity") or ""),
        "is_first_edition": "1st Edition" in subtypes,
        "image_url":        api_card.get("images", {}).get("large"),
        # set info
        "set_name":         api_card["set"]["name"],
        "set_code":         api_card["set"]["id"],
        "series":           api_card["set"].get("series"),
        "release_year":     _parse_year(api_card["set"].get("releaseDate")),
        "total_cards":      api_card["set"].get("total"),
    }


def parse_card_attribute_fields(api_card: dict) -> dict:
    """Extract card_attributes fields from a PokemonTCG API card object."""
    types      = api_card.get("types", [])
    weaknesses = api_card.get("weaknesses", [])
    resistances= api_card.get("resistances", [])
    hp_str     = api_card.get("hp")

    return {
        "card_type":    api_card.get("supertype"),
        "stage":        _detect_stage(api_card),
        "hp":           int(hp_str) if hp_str and hp_str.isdigit() else None,
        "energy_type":  types[0] if types else None,
        "artist":       api_card.get("artist"),
        "weakness":     weaknesses[0]["type"] if weaknesses else None,
        "resistance":   resistances[0]["type"] if resistances else None,
        "retreat_cost": len(api_card.get("retreatCost", [])),
    }


# ----------------------------------------------------------------
# Internal helpers
# ----------------------------------------------------------------

def _detect_variant(api_card: dict) -> str | None:
    subtypes = api_card.get("subtypes", [])
    rarity   = api_card.get("rarity", "") or ""
    name     = api_card.get("name", "") or ""

    if "Reverse Holo" in subtypes:
        return "Reverse Holo"
    if "Amazing Rare" in rarity:
        return "Amazing Rare"
    if "Rainbow Rare" in rarity or "Rainbow" in rarity:
        return "Rainbow Rare"
    if "Full Art" in rarity or "Full Art" in subtypes:
        return "Full Art"
    if "Secret" in rarity:
        return "Secret Rare"
    if "Shadowless" in subtypes or "shadowless" in name.lower():
        return "Shadowless"
    if "1st Edition" in subtypes:
        return "1st Edition"
    return None


def _detect_finish(api_card: dict) -> str | None:
    subtypes = api_card.get("subtypes", [])
    if "VMAX" in subtypes or "VSTAR" in subtypes:
        return "Holo"
    if "Holo Rare" in (api_card.get("rarity") or ""):
        return "Holo"
    return "Non-Holo"


def _detect_stage(api_card: dict) -> str | None:
    subtypes = api_card.get("subtypes", [])
    for stage in ("Basic", "Stage 1", "Stage 2", "VMAX", "VSTAR", "V", "GX", "EX"):
        if stage in subtypes:
            return stage
    return None


def _parse_year(date_str: str | None) -> int | None:
    if date_str and len(date_str) >= 4:
        try:
            return int(date_str[:4])
        except ValueError:
            pass
    return None
