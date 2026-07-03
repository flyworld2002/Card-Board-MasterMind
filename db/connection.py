"""
db/connection.py
Handles PostgreSQL connection via psycopg2.
Reads credentials from .env file.
"""

import os
import psycopg2
import psycopg2.extras
from contextlib import contextmanager
from dotenv import load_dotenv

load_dotenv()


def get_connection():
    """Return a raw psycopg2 connection."""
    return psycopg2.connect(
        host=os.getenv("DB_HOST"),
        port=os.getenv("DB_PORT", 5432),
        dbname=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        sslmode=os.getenv("DB_SSLMODE", "require"),
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


@contextmanager
def db_cursor():
    """Context manager: yields a cursor, commits on success, rolls back on error."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ----------------------------------------------------------------
# card_games
# ----------------------------------------------------------------

def get_game_id(name: str = "Pokemon") -> str:
    with db_cursor() as cur:
        cur.execute("SELECT id FROM card_games WHERE name = %s", (name,))
        row = cur.fetchone()
        if not row:
            raise ValueError(f"Game '{name}' not found in card_games table.")
        return str(row["id"])


# ----------------------------------------------------------------
# card_sets
# ----------------------------------------------------------------

def get_or_create_set(game_id: str, name: str, set_code: str,
                      series: str = None, release_year: int = None,
                      language: str = "English", total_cards: int = None) -> str:
    with db_cursor() as cur:
        cur.execute(
            "SELECT id FROM card_sets WHERE set_code = %s AND language = %s",
            (set_code, language)
        )
        row = cur.fetchone()
        if row:
            return str(row["id"])

        cur.execute("""
            INSERT INTO card_sets (game_id, name, series, set_code, release_year, language, total_cards)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (game_id, name, series, set_code, release_year, language, total_cards))
        return str(cur.fetchone()["id"])


# ----------------------------------------------------------------
# card_master
# ----------------------------------------------------------------

def find_card_by_external_id(external_id: str) -> dict | None:
    with db_cursor() as cur:
        cur.execute("SELECT * FROM card_master WHERE external_id = %s", (external_id,))
        return cur.fetchone()


def find_card_by_name_set(name: str, set_id: str, variant: str = None) -> list[dict]:
    # NOTE: card_master no longer has a `variant` column (variants live in
    # card_variants under the seven-axis model). The `variant` parameter is
    # accepted for backward-compatibility but ignored.
    with db_cursor() as cur:
        cur.execute("""
            SELECT * FROM card_master
            WHERE set_id = %s
              AND LOWER(name) = LOWER(%s)
        """, (set_id, name))
        return cur.fetchall()


def insert_card_master(set_id: str, name: str, card_number: str,
                       rarity: str = None, variant: str = None,
                       finish: str = None, is_promo: bool = False,
                       is_first_edition: bool = False, image_url: str = None,
                       external_id: str = None) -> str:
    with db_cursor() as cur:
        cur.execute("""
            INSERT INTO card_master
                (set_id, name, card_number, rarity,
                 is_promo, is_first_edition, image_url, external_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (external_id) DO UPDATE
                SET name      = EXCLUDED.name,
                    rarity    = EXCLUDED.rarity,
                    image_url = EXCLUDED.image_url
            RETURNING id
        """, (set_id, name, card_number, rarity,
              is_promo, is_first_edition, image_url, external_id))
        return str(cur.fetchone()["id"])

def insert_card_attributes(card_id: str, card_type: str = None, stage: str = None,
                           hp: int = None, energy_type: str = None, artist: str = None,
                           weakness: str = None, resistance: str = None,
                           retreat_cost: int = None):
    with db_cursor() as cur:
        cur.execute("""
            INSERT INTO card_attributes
                (card_id, card_type, stage, hp, energy_type, artist, weakness, resistance, retreat_cost)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (card_id) DO NOTHING
        """, (card_id, card_type, stage, hp, energy_type, artist, weakness, resistance, retreat_cost))


# ----------------------------------------------------------------
# purchases
# ----------------------------------------------------------------

def insert_purchase(source: str, purchase_type: str, total_cost: float,
                    card_count: int = None, reference_id: str = None,
                    notes: str = None, purchased_at=None) -> str:
    from datetime import datetime, timezone
    purchased_at = purchased_at or datetime.now(timezone.utc)
    with db_cursor() as cur:
        cur.execute("""
            INSERT INTO purchases
                (source, purchase_type, reference_id, total_cost, card_count, notes, purchased_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            RETURNING id
        """, (source, purchase_type, reference_id, total_cost, card_count, notes, purchased_at))
        return str(cur.fetchone()["id"])


# ----------------------------------------------------------------
# import corrections
# ----------------------------------------------------------------

def get_import_corrections() -> list[dict]:
    """Load all import corrections into memory for fast lookup during import."""
    with db_cursor() as cur:
        cur.execute("SELECT * FROM import_corrections ORDER BY card_name")
        return cur.fetchall()


def apply_import_correction(item: dict, corrections: list[dict]) -> dict:
    """
    Check if an item matches any correction rule and apply it.
    Matches on card_name + optional card_number + optional set_name.
    """
    for c in corrections:
        name_match = c["card_name"].lower() == item["card_name"].lower()
        num_match  = (not c["card_number"] or
                      c["card_number"] == item.get("card_number"))
        set_match  = (not c["set_name"] or
                      c["set_name"].lower() == item.get("set_name", "").lower())

        if name_match and num_match and set_match:
            if c.get("correct_card_name"):
                item["card_name"] = c["correct_card_name"]
            if c.get("correct_number"):
                item["card_number"] = c["correct_number"]
            if c.get("correct_foil_type"):
                item["foil_type"] = c["correct_foil_type"]
            if c.get("correct_foil_pattern"):
                item["foil_pattern"] = c["correct_foil_pattern"]
            break
    return item


# ----------------------------------------------------------------
# card_variants
# ----------------------------------------------------------------

def get_or_create_variant(card_id: str,
                          foil_type: str = None,
                          foil_pattern: str = None,
                          texture: str = None,
                          material: str = None,
                          size: str = None,
                          stamp_type: str = None,
                          source_type: str = None) -> str:
    """
    Get existing or create new card_variant using the seven-axis model.
    All axes are nullable lookup codes (NULL = standard/none).

    Identity is the generated variant_key (a null-safe concatenation of all
    seven axes). The DB enforces uniqueness on (card_id, variant_key); we match
    on the axes directly with NULL-safe IS NOT DISTINCT FROM, and rely on
    ON CONFLICT for the race-safe insert.

    Returns variant UUID.
    """
    with db_cursor() as cur:
        cur.execute("""
            SELECT id FROM card_variants
            WHERE card_id = %s
              AND foil_type    IS NOT DISTINCT FROM %s
              AND foil_pattern IS NOT DISTINCT FROM %s
              AND texture      IS NOT DISTINCT FROM %s
              AND material     IS NOT DISTINCT FROM %s
              AND size         IS NOT DISTINCT FROM %s
              AND stamp_type   IS NOT DISTINCT FROM %s
              AND source_type  IS NOT DISTINCT FROM %s
        """, (card_id, foil_type, foil_pattern, texture,
              material, size, stamp_type, source_type))
        row = cur.fetchone()
        if row:
            return str(row["id"])

        # variant_key is GENERATED; unique constraint is (card_id, variant_key).
        cur.execute("""
            INSERT INTO card_variants
                (card_id, foil_type, foil_pattern, texture,
                 material, size, stamp_type, source_type)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (card_id, variant_key) DO UPDATE
                SET card_id = EXCLUDED.card_id
            RETURNING id
        """, (card_id, foil_type, foil_pattern, texture,
              material, size, stamp_type, source_type))
        return str(cur.fetchone()["id"])


def upsert_market_price(variant_id: str, condition: str,
                        market_price: float, source: str = "tcgplayer"):
    """Insert or update market price for a variant+condition."""
    with db_cursor() as cur:
        cur.execute("""
            INSERT INTO market_prices (variant_id, condition, market_price, source, updated_at)
            VALUES (%s, %s, %s, %s, NOW())
            ON CONFLICT (variant_id, condition)
            DO UPDATE SET market_price = EXCLUDED.market_price,
                          source       = EXCLUDED.source,
                          updated_at   = NOW()
        """, (variant_id, condition, market_price, source))


def get_market_price(variant_id: str, condition: str) -> float | None:
    """Get current market price for a variant+condition."""
    with db_cursor() as cur:
        cur.execute("""
            SELECT market_price FROM market_prices
            WHERE variant_id = %s AND condition = %s
        """, (variant_id, condition))
        row = cur.fetchone()
        return float(row["market_price"]) if row else None


# ----------------------------------------------------------------
# inventory
# ----------------------------------------------------------------

def insert_inventory(card_id: str, purchase_id: str, condition: str,
                     quantity: int, cost_basis: float, asking_price: float = None,
                     is_graded: bool = False, notes: str = None, acquired_at=None,
                     variant_id: str = None) -> str:
    from datetime import datetime, timezone
    acquired_at = acquired_at or datetime.now(timezone.utc)
    with db_cursor() as cur:
        cur.execute("""
            INSERT INTO inventory
                (card_id, purchase_id, condition, is_graded, quantity,
                 cost_basis, asking_price, notes, acquired_at, variant_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id
        """, (card_id, purchase_id, condition, is_graded, quantity,
              cost_basis, asking_price, notes, acquired_at, variant_id))
        return str(cur.fetchone()["id"])


def get_stock_summary(card_id: str = None) -> list[dict]:
    """Return available quantity per card+condition+variant, averaged cost."""
    with db_cursor() as cur:
        query = """
            SELECT
                cm.name              AS card_name,
                cs.name              AS set_name,
                cm.card_number,
                cs.base_set_number,
                cm.is_promo,
                CASE
                    WHEN cs.base_set_number IS NOT NULL AND NOT cm.is_promo
                    THEN cm.card_number || '/' || cs.base_set_number
                    ELSE cm.card_number
                END                  AS display_number,
                cv.foil_type,
                cv.foil_pattern,
                cv.texture,
                cv.material,
                cv.size,
                cv.stamp_type        AS variant_stamp_type,
                cv.source_type       AS variant_source_type,
                cv.variant_key,
                i.condition,
                SUM(i.quantity - i.quantity_sold)                AS qty_available,
                ROUND(SUM(i.cost_basis * i.quantity) /
                      NULLIF(SUM(i.quantity), 0), 2)             AS avg_cost_basis,
                MAX(i.asking_price)                              AS asking_price,
                MAX(mp.market_price)                             AS market_price,
                MAX(mp.updated_at)                               AS market_price_updated_at
            FROM inventory i
            JOIN card_master cm      ON i.card_id    = cm.id
            JOIN card_sets   cs      ON cm.set_id    = cs.id
            LEFT JOIN card_variants cv  ON i.variant_id = cv.id
            LEFT JOIN market_prices mp  ON mp.variant_id = i.variant_id
                                       AND mp.condition  = i.condition
            WHERE (i.quantity - i.quantity_sold) > 0
        """
        params = []
        if card_id:
            query += " AND i.card_id = %s"
            params.append(card_id)
        query += """ GROUP BY cm.name, cs.name, cm.card_number, cs.base_set_number,
                              cm.is_promo, cv.foil_type, cv.foil_pattern, cv.texture,
                              cv.material, cv.size, cv.stamp_type, cv.source_type,
                              cv.variant_key, i.condition
                     ORDER BY cm.name"""
        cur.execute(query, params)
        return cur.fetchall()
