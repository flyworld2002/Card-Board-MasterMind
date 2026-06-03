"""
utils/pricing_engine.py
Calculates list price for a card based on 4-layer hierarchy:

  1. Card-level manual override  (highest priority)
  2. Set-level config (multiplier + floor prices)
  3. Price tier system (market price → list price)
  4. Platform default fallback

Usage:
    from utils.pricing_engine import calculate_price
    price = calculate_price(card_id, market_price, rarity, platform='ebay')
"""

from db.connection import db_cursor


# ----------------------------------------------------------------
# Card type classification
# Maps rarity strings from PokemonTCG API → our internal type
# ----------------------------------------------------------------

RARITY_TO_TYPE = {
    # Commons / non-holo
    "common":                    "common",
    "uncommon":                  "common",
    "rare":                      "holo",
    "rare holo":                 "holo",
    "rare holo v":               "holo",
    "rare holo vmax":            "holo",
    "rare holo vstar":           "holo",
    "double rare":               "holo",
    "trainer gallery rare holo": "holo",
    # Reverse holo
    "reverse holo":              "reverse_holo",
    # Ultra rares and above
    "illustration rare":         "ultra_rare",
    "special illustration rare": "ultra_rare",
    "ultra rare":                "ultra_rare",
    "hyper rare":                "ultra_rare",
    "rare rainbow":              "ultra_rare",
    "rare secret":               "ultra_rare",
    "amazing rare":              "ultra_rare",
    "rare shiny":                "ultra_rare",
    "shiny rare":                "ultra_rare",
    "shiny ultra rare":          "ultra_rare",
    "ace spec rare":             "ultra_rare",
    "rare ace":                  "ultra_rare",
}


def get_card_type(rarity: str, variant: str = None) -> str:
    """Map rarity + variant to internal card type."""
    if variant and "reverse" in variant.lower():
        return "reverse_holo"
    rarity_lower = (rarity or "").lower()
    return RARITY_TO_TYPE.get(rarity_lower, "common")


# ----------------------------------------------------------------
# Main pricing function
# ----------------------------------------------------------------

def calculate_price(card_id: str, market_price: float,
                    rarity: str, variant: str = None,
                    set_id: str = None,
                    platform: str = "ebay") -> dict:
    """
    Calculate list price for a card.

    Returns dict with:
        list_price:   final recommended price
        rule_used:    which layer determined the price
        breakdown:    step-by-step explanation
    """
    market_price = market_price or 0.0
    card_type    = get_card_type(rarity, variant)

    # Layer 1: Card-level manual override
    override = _get_card_override(card_id, platform)
    if override:
        return {
            "list_price": override,
            "rule_used":  "card_override",
            "breakdown":  f"Manual override: ${override:.2f}",
        }

    # Layer 2: Set-level config
    set_config = _get_set_config(set_id, platform) if set_id else None

    # Layer 3: Tier-based price
    tier_price = _apply_tiers(market_price, card_type, platform)

    # Ultra rare special handling
    if card_type == "ultra_rare":
        tier_price = _price_ultra_rare(
            market_price, set_config, card_type, platform
        )

    # Apply set multiplier and floor prices
    final_price = tier_price
    rule_used   = "tier"
    breakdown   = f"Market ${market_price:.2f} → tier ${tier_price:.2f}"

    if set_config:
        multiplier = float(set_config.get("price_multiplier", 1.0))
        if multiplier != 1.0:
            final_price = round(tier_price * multiplier, 2)
            rule_used   = "set_multiplier"
            breakdown  += f" × {multiplier} = ${final_price:.2f}"

        # Apply floor prices
        floor = _get_floor(set_config, card_type)
        if floor and final_price < floor:
            final_price = floor
            rule_used   = "set_floor"
            breakdown  += f" (floor ${floor:.2f} applied)"

    return {
        "list_price": final_price,
        "rule_used":  rule_used,
        "breakdown":  breakdown,
    }


# ----------------------------------------------------------------
# Layer helpers
# ----------------------------------------------------------------

def _get_card_override(card_id: str, platform: str) -> float | None:
    """Check for a manual card-level price override."""
    if not card_id or card_id.startswith("dry"):
        return None
    with db_cursor() as cur:
        cur.execute("""
            SELECT list_price FROM card_pricing_overrides
            WHERE card_id = %s AND platform = %s
        """, (card_id, platform))
        row = cur.fetchone()
        return float(row["list_price"]) if row else None


def _get_set_config(set_id: str, platform: str) -> dict | None:
    """Fetch set-level pricing config."""
    if not set_id:
        return None
    with db_cursor() as cur:
        cur.execute("""
            SELECT * FROM set_pricing_config
            WHERE set_id = %s AND platform = %s
        """, (set_id, platform))
        return cur.fetchone()


def _apply_tiers(market_price: float, card_type: str,
                 platform: str) -> float:
    """Apply tier system to get base list price."""
    with db_cursor() as cur:
        cur.execute("""
            SELECT list_price FROM price_tiers
            WHERE platform  = %s
              AND card_type  = %s
              AND market_price_max >= %s
            ORDER BY market_price_max ASC
            LIMIT 1
        """, (platform, card_type, market_price))
        row = cur.fetchone()
        if row:
            return float(row["list_price"])

    # Above all tiers: market × 2 + $1
    return round(market_price * 2 + 1.0, 2)


def _price_ultra_rare(market_price: float, set_config: dict | None,
                      card_type: str, platform: str) -> float:
    """Price ultra rare cards based on set config rule."""
    if set_config:
        rule = set_config.get("ultra_rare_rule", "tier")
        if rule == "multiplier":
            multiplier = float(set_config.get("ultra_rare_multiplier", 2.0))
            plus       = float(set_config.get("ultra_rare_plus", 1.0))
            return round(market_price * multiplier + plus, 2)
        elif rule == "manual":
            # Manual means use card override — return tier as suggestion
            return _apply_tiers(market_price, card_type, platform)

    # Default: market × 2 + $1
    return round(market_price * 2 + 1.0, 2)


def _get_floor(set_config: dict, card_type: str) -> float | None:
    """Get floor price for a card type from set config."""
    floor_map = {
        "common":       "common_floor",
        "holo":         "holo_floor",
        "reverse_holo": "reverse_holo_floor",
    }
    field = floor_map.get(card_type)
    if field and set_config.get(field):
        return float(set_config[field])
    return None


# ----------------------------------------------------------------
# Bulk pricing — calculate prices for all cards in staging
# ----------------------------------------------------------------

def price_staging_batch(batch_id: str, platform: str = "ebay"):
    """
    Calculate and store recommended prices for all cards in a staging batch.
    Populates staging.calculated_price for each matched row.
    """
    with db_cursor() as cur:
        cur.execute("""
            SELECT
                s.id, s.card_id, s.condition,
                cm.rarity, cm.set_id,
                i_avg.avg_market
            FROM staging s
            LEFT JOIN card_master cm ON s.card_id = cm.id
            LEFT JOIN (
                SELECT cv.card_id, AVG(mp.market_price) AS avg_market
                FROM market_prices mp
                JOIN card_variants cv ON mp.variant_id = cv.id
                WHERE mp.market_price IS NOT NULL
                GROUP BY cv.card_id
            ) i_avg ON i_avg.card_id = s.card_id
            WHERE s.import_batch = %s
              AND s.card_id IS NOT NULL
              AND s.status = 'pending'
        """, (batch_id,))
        rows = cur.fetchall()

    updates = []
    for row in rows:
        market = float(row["avg_market"] or 0)
        result = calculate_price(
            card_id      = str(row["card_id"]),
            market_price = market,
            rarity       = row.get("rarity", ""),
            variant      = row.get("variant"),
            set_id       = str(row["set_id"]) if row.get("set_id") else None,
            platform     = platform,
        )
        updates.append((result["list_price"], result["breakdown"], str(row["id"])))

    if updates:
        with db_cursor() as cur:
            cur.executemany("""
                UPDATE staging
                SET calculated_price = %s,
                    notes = COALESCE(notes || ' | ', '') || %s,
                    updated_at = NOW()
                WHERE id = %s
            """, updates)
