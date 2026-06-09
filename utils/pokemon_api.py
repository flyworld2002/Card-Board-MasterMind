"""
utils/pokemon_api.py
Looks up card data from the free PokemonTCG API (pokemontcg.io).
No API key required for basic usage (rate-limited to 1000 req/day).
Set POKEMONTCG_API_KEY in .env for higher limits.
"""

import os
import re
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from functools import lru_cache

# Module-level lookup cache — persists for one import run
_ebay_lookup_cache: dict = {}

def rarity_to_card_type(rarity: str) -> str:
    """Map Pokemon TCG API rarity to our card_type classifier."""
    if not rarity:
        return "common"
    r = rarity.lower()
    if any(x in r for x in ("special illustration", "hyper", "ace spec")):
        return "ultra_rare"
    if any(x in r for x in ("illustration rare", "secret")):
        return "ultra_rare"
    if any(x in r for x in ("double rare", "ultra rare")):
        return "holo"
    if "rare holo" in r or "rare" in r:
        return "holo"
    if "promo" in r:
        return "promo"
    return "common"


def _session():
    """Requests session with automatic retry on timeout/connection errors."""

def _session():
    """Requests session with automatic retry on timeout/connection errors."""
    s = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=2,
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
        params={"q": f'name:"{name}"'},          # FIX: was referencing undefined search_name
        headers=_headers(),
        timeout=30
    )
    resp.raise_for_status()
    data = resp.json().get("data", [])
    return data[0] if data else None


@lru_cache(maxsize=256)
def get_set_by_code(code: str) -> dict | None:
    """Find a set by its code (e.g. 'base1', 'sv3')."""
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
            continue
        if len(results) == 1:
            return results

    # Pass 3: name + set ID (no number)
    if api_set_id:
        results = _api_search(f'name:"{search_name}" set.id:{api_set_id}')
        if results:
            if nidoran_filter:
                filtered = [c for c in results if nidoran_filter in c["name"]]
                if filtered:
                    return [filtered[0]]
            return [_sort_by_number(results)[0]]

    # Pass 4: name only
    results = _api_search(f'name:"{search_name}"')
    if results and api_set_id:
        filtered = [c for c in results if c["set"]["id"] == api_set_id]
        if filtered:
            return [_sort_by_number(filtered)[0]]
        return []

    if results:
        return [_sort_by_number(results)[0]]

    return []


# ----------------------------------------------------------------
# eBay import: auto-match a parsed variation to card_master
# ----------------------------------------------------------------

def lookup_card_for_ebay(card_name: str, card_number: str,
                         set_name: str, variant_type: str) -> dict:
    """
    Given a parsed eBay variation, find or create the matching card_master row.

    Returns:
    {
        "card_id":      UUID string (or None if not found),
        "variant_id":   UUID string (or None),
        "matched":      True/False,
        "source":       "db_exact" | "db_fuzzy" | "api" | "not_found",
        "card_name":    canonical name from API/DB,
        "set_name":     canonical set name,
        "rarity":       rarity string,
        "image_url":    card image URL,
        "api_card_id":  external_id e.g. "sv3-153",
    }
    """

    cache_key = f"{set_name}|{card_number}|{card_name}"
    if cache_key in _ebay_lookup_cache:
        cached = _ebay_lookup_cache[cache_key]
        if not cached["matched"]:
            print(f"    ⚠️  CACHE HIT not_found: {cache_key}")
        return cached

    from db.connection import (
        find_card_by_external_id, find_card_by_name_set,
        get_game_id, get_or_create_set, insert_card_master,
        insert_card_attributes, get_or_create_variant, db_cursor,
    )
    from utils.set_name_map import get_set_id

    result = {
        "card_id":    None,
        "variant_id": None,
        "matched":    False,
        "source":     "not_found",
        "card_name":  card_name,
        "set_name":   set_name,
        "rarity":     None,
        "image_url":  None,
        "api_card_id": None,
        "_api_card":  None,    # full card data — avoids extra API call for market price
    }

    # ── Step 1: Search the Pokemon TCG API ───────────────────────────────────

    # Normalize curly apostrophes to straight apostrophes
    card_name  = card_name.replace('\u2019', "'").replace('\u2018', "'")

    # Strip variant suffixes from card name before API lookup
    clean_name = re.sub(
        r'\s+(Reverse\s+Holo|Holo|RH|Promo|Black\s+Star\s+Promo)$',
        '', card_name, flags=re.IGNORECASE
    ).strip()

    # Name corrections — accent-stripped or common misspellings
    NAME_CORRECTIONS = {
        "flabebe":           "Flabébé",
        "flabébé":           "Flabébé",
        "nidoran f":         "Nidoran ♀",
        "nidoran m":         "Nidoran ♂",
        "poke vital a":      "Poké Vital A",
        "poke vital b":      "Poké Vital B",
        "poke vital":        "Poké Vital",
        "billy and o'nare":  "Billy & O'Nare",
        # SVE energy name corrections
        "leaf energy":       "Basic Grass Energy",
        "fire energy":       "Basic Fire Energy",
        "water energy":      "Basic Water Energy",
        "lightning energy":  "Basic Lightning Energy",
        "psychic energy":    "Basic Psychic Energy",
        "fighting energy":   "Basic Fighting Energy",
        "dark energy":       "Basic Darkness Energy",
        "metal energy":      "Basic Metal Energy",
        "professor's research (sada)":  "Professor's Research",
        "professor's research (turo)":  "Professor's Research",
    }
    if clean_name.lower() in NAME_CORRECTIONS:
        clean_name = NAME_CORRECTIONS[clean_name.lower()]

    api_results = search_cards(
        name=clean_name,
        set_name=set_name,
        card_number=card_number,
    )

    if not api_results:
        print(f"    ⚠️  Not found in Pokemon TCG API: {card_name} #{card_number} ({set_name})")
        return result

    api_card = api_results[0]
    external_id = api_card["id"]
    result["api_card_id"] = external_id
    result["card_name"]   = api_card["name"]
    result["set_name"]    = api_card["set"]["name"]
    result["rarity"]      = api_card.get("rarity")
    result["image_url"]   = api_card.get("images", {}).get("large")
    result["_api_card"]   = api_card    # cache full card — no extra call needed for market price

    # ── Step 2: Check if card already exists in card_master ──────────────────
    existing = find_card_by_external_id(external_id)
    if existing:
        card_id = str(existing["id"])
        result["card_id"] = card_id
        result["matched"] = True
        result["source"]  = "db_exact"
    else:
        # ── Step 3: Create card_master row from API data ──────────────────────
        try:
            game_id     = get_game_id("Pokemon")
            fields      = parse_card_master_fields(api_card)
            attr_fields = parse_card_attribute_fields(api_card)

            set_id = get_or_create_set(
                game_id      = game_id,
                name         = fields["set_name"],
                set_code     = fields["set_code"],
                series       = fields.get("series"),
                release_year = fields.get("release_year"),
                total_cards  = fields.get("total_cards"),
            )

            card_id = insert_card_master(
                set_id           = set_id,
                name             = fields["name"],
                card_number      = fields["card_number"],
                rarity           = fields.get("rarity"),
                variant          = fields.get("variant"),
                finish           = fields.get("finish"),
                is_promo         = fields.get("is_promo", False),
                is_first_edition = fields.get("is_first_edition", False),
                image_url        = fields.get("image_url"),
                external_id      = fields["external_id"],
            )
            insert_card_attributes(card_id, **attr_fields)

            result["card_id"] = card_id
            result["matched"] = True
            result["source"]  = "api"

        except Exception as e:
            print(f"    ❌ Error creating card_master for {card_name}: {e}")
            return result

    # ── Step 4: Get or create card_variant ───────────────────────────────────
    try:
        SPECIAL_PATTERNS = {
            "Reverse Holo", "Cosmos Holo", "Master Ball Pattern",
            "Poke Ball Pattern", "Cracked Ice Holo", "Galaxy Holo"
        }
        is_special   = variant_type in SPECIAL_PATTERNS
        finish       = variant_type if variant_type != "Normal" else "Non-Holo"
        variant_id   = get_or_create_variant(
            card_id      = result["card_id"],
            variant_type = variant_type if variant_type != "Normal" else "Non-Holo",
            finish       = finish,
            is_special   = is_special,
            source_type  = result.get("source_type"),
            stamp_type   = result.get("stamp_type"),
        )
        result["variant_id"] = variant_id
    except Exception as e:
        print(f"    ⚠️  Could not create variant for {card_name}: {e}")

    _ebay_lookup_cache[cache_key] = result
    return result


# ----------------------------------------------------------------
# Market price extraction from cached api_card (no extra API call)
# ----------------------------------------------------------------

def extract_market_price(api_card: dict, variant_type: str) -> tuple[float | None, str | None]:
    """
    Extract market price and its date from a cached api_card object.
    Returns (price, date_str) tuple.
    date_str format: 'YYYY/MM/DD' from TCGPlayer updatedAt field.
    """
    if not api_card:
        return None, None

    tcgplayer  = api_card.get("tcgplayer", {})
    tcg_prices = tcgplayer.get("prices", {})
    price_date = tcgplayer.get("updatedAt")    # ← e.g. "2026/06/03"

    if not tcg_prices:
        return None, None

    PRICE_KEYS = {
        "Reverse Holo":        ["reverseHolofoil", "holofoil", "normal"],
        "Holo":                ["holofoil", "normal"],
        "Cosmos Holo":         ["holofoil", "normal"],
        "Master Ball Pattern": ["holofoil", "normal"],
        "Normal":              ["normal", "holofoil", "reverseHolofoil"],
        "promo":               ["holofoil", "normal"],
    }
    keys = PRICE_KEYS.get(variant_type, ["normal", "holofoil", "reverseHolofoil"])

    for key in keys:
        if key in tcg_prices:
            market = tcg_prices[key].get("market") or tcg_prices[key].get("mid")
            if market:
                return float(market), price_date

    return None, None


# ----------------------------------------------------------------
# Market price lookup (used by staging_workflow on approve)
# ----------------------------------------------------------------

def get_market_price_from_api(card_id: str, condition: str) -> float | None:
    """
    Fetch current market price for a card from the Pokemon TCG API.
    Looks up by external_id stored in card_master, reads TCGPlayer prices.

    Condition mapping:
        Near Mint         → normal
        Lightly Played    → 1stEditionNormal (closest available)
        Moderately Played → reverseHolofoil (proxy)
        Heavily Played    → (no direct map — returns None)
    """
    from db.connection import db_cursor

    # Get external_id for this card_id
    with db_cursor() as cur:
        cur.execute("SELECT external_id FROM card_master WHERE id = %s", (card_id,))
        row = cur.fetchone()
        if not row or not row["external_id"]:
            return None
        external_id = row["external_id"]

    # Fetch card from API
    api_card = get_card_by_id(external_id)
    if not api_card:
        return None

    # Pull TCGPlayer prices
    tcgplayer = api_card.get("tcgplayer", {})
    prices    = tcgplayer.get("prices", {})

    # Condition → price key mapping
    CONDITION_MAP = {
        "Near Mint":          ["normal", "holofoil", "reverseHolofoil", "1stEditionNormal"],
        "Lightly Played":     ["normal", "holofoil", "reverseHolofoil"],
        "Moderately Played":  ["normal", "holofoil"],
        "Heavily Played":     ["normal"],
        "Damaged":            ["normal"],
    }

    price_keys = CONDITION_MAP.get(condition, ["normal"])
    for key in price_keys:
        if key in prices:
            market = prices[key].get("market") or prices[key].get("mid")
            if market:
                return float(market)

    return None


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
        "set_name":         api_card["set"]["name"],
        "set_code":         api_card["set"]["id"],
        "series":           api_card["set"].get("series"),
        "release_year":     _parse_year(api_card["set"].get("releaseDate")),
        "total_cards":      api_card["set"].get("total"),
    }


def parse_card_attribute_fields(api_card: dict) -> dict:
    """Extract card_attributes fields from a PokemonTCG API card object."""
    types       = api_card.get("types", [])
    weaknesses  = api_card.get("weaknesses", [])
    resistances = api_card.get("resistances", [])
    hp_str      = api_card.get("hp")

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

def _normalize_name(name: str) -> str:
    return (name.lower()
            .replace("♀", " f").replace("♂", " m")
            .replace("-", " ").strip())


def _sort_by_number(cards: list) -> list:
    def num_key(c):
        n = c.get("number", "9999")
        m = re.match(r'(\d+)', str(n))
        return int(m.group(1)) if m else 9999
    return sorted(cards, key=num_key)


def _filter_by_variant(results: list, variant: str) -> list:
    variant_lower = variant.lower()
    filtered = []
    for c in results:
        subtypes        = " ".join(c.get("subtypes", [])).lower()
        card_name_lower = c.get("name", "").lower()
        if variant_lower in card_name_lower:
            filtered.append(c)
        elif variant_lower in subtypes:
            filtered.append(c)
        elif variant_lower in c.get("set", {}).get("name", "").lower():
            filtered.append(c)
    return filtered


def _api_search(q: str, page_size: int = 20) -> list[dict]:
    import time
    max_retries = 3
    retry_delay = 5  # seconds between retries

    for attempt in range(1, max_retries + 1):
        try:
            resp = _session().get(
                f"{BASE_URL}/cards",
                params={"q": q, "pageSize": page_size},
                headers=_headers(),
                timeout=30
            )
            if resp.status_code == 404:
                return []
            resp.raise_for_status()
            return resp.json().get("data", [])

        except Exception as e:
            if attempt < max_retries:
                print(f"    ⏳ API timeout (attempt {attempt}/{max_retries}), retrying in {retry_delay}s...")
                time.sleep(retry_delay)
            else:
                print(f"    ❌ API failed after {max_retries} attempts: {e}")
                raise


def get_card_by_id(card_id: str) -> dict | None:
    """Fetch a specific card by its API ID (e.g. 'sv3-4')."""
    resp = _session().get(
        f"{BASE_URL}/cards/{card_id}",
        headers=_headers(),
        timeout=30
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json().get("data")


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
