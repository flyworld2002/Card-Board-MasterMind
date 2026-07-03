"""
db/staging.py
Database operations for the staging table.
"""

from datetime import datetime, timezone
from db.connection import db_cursor


def create_batch_id() -> str:
    """Generate a unique batch ID based on current timestamp."""
    return datetime.now(timezone.utc).strftime("batch_%Y%m%d_%H%M%S")


def insert_staging_row(batch_id: str, order_number: str, order_date,
                       card_name: str, set_name: str, condition: str,
                       quantity: int, price: float, source: str = "tcgplayer",
                       card_id: str = None, match_status: str = "pending",
                       match_options: list = None, foil_type: str = None,
                       foil_pattern: str = None, texture: str = None,
                       material: str = None, size: str = None,
                       stamp_type: str = None, source_type: str = None,
                       is_shiny: bool = False,
                       variation_name: str = None,
                       listing_price: float = None) -> str:
    """Insert one card into staging. Returns the staging row UUID."""
    import json
    with db_cursor() as cur:
        cur.execute("""
            INSERT INTO staging
                (import_batch, order_number, order_date, source,
                 card_name, set_name, condition,
                 foil_type, foil_pattern, texture, material, size,
                 stamp_type, source_type, is_shiny, variation_name,
                 listing_price,
                 quantity, price, card_id, match_status, match_options)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id
        """, (
            batch_id, order_number, order_date, source,
            card_name, set_name, condition,
            foil_type, foil_pattern, texture, material, size,
            stamp_type, source_type, is_shiny, variation_name,
            listing_price,
            quantity, price, card_id, match_status,
            json.dumps(match_options) if match_options else None
        ))
        return str(cur.fetchone()["id"])


def get_pending_batches() -> list[dict]:
    """Return all batches that have pending rows."""
    with db_cursor() as cur:
        cur.execute("""
            SELECT
                import_batch,
                source,
                MIN(order_date)     AS earliest_order,
                MAX(order_date)     AS latest_order,
                COUNT(*)            AS total_rows,
                SUM(CASE WHEN status = 'pending'  THEN 1 ELSE 0 END) AS pending,
                SUM(CASE WHEN status = 'approved' THEN 1 ELSE 0 END) AS approved,
                SUM(CASE WHEN status = 'skipped'  THEN 1 ELSE 0 END) AS skipped,
                MIN(created_at)     AS imported_at
            FROM staging
            GROUP BY import_batch, source
            HAVING SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) > 0
            ORDER BY MIN(created_at) DESC
        """)
        return cur.fetchall()


def get_staging_rows(batch_id: str = None,
                     status: str = "pending") -> list[dict]:
    """Fetch staging rows, optionally filtered by batch and status."""
    with db_cursor() as cur:
        query = """
            SELECT
                s.*,
                cm.name         AS matched_card_name,
                cm.card_number  AS matched_card_number,
                cm.rarity,
                cm.variant,
                cs.name         AS matched_set_name,
                cs.set_code
            FROM staging s
            LEFT JOIN card_master cm ON s.card_id = cm.id
            LEFT JOIN card_sets   cs ON cm.set_id = cs.id
            WHERE 1=1
        """
        params = []
        if batch_id:
            query += " AND s.import_batch = %s"
            params.append(batch_id)
        if status:
            query += " AND s.status = %s"
            params.append(status)
        query += " ORDER BY s.order_date ASC, s.card_name ASC"
        cur.execute(query, params)
        return cur.fetchall()


def update_staging_row(row_id: str, **kwargs):
    """Update fields on a staging row."""
    allowed = {"condition", "quantity", "price", "card_id", "status",
               "notes", "override_price", "match_status"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return
    set_clause = ", ".join(f"{k} = %s" for k in updates)
    values     = list(updates.values()) + [row_id]
    with db_cursor() as cur:
        cur.execute(
            f"UPDATE staging SET {set_clause}, updated_at = NOW() WHERE id = %s",
            values
        )


def approve_batch(batch_id: str) -> list[dict]:
    """Return all approved rows in a batch, ready to push to inventory."""
    with db_cursor() as cur:
        cur.execute("""
            SELECT * FROM staging
            WHERE import_batch = %s AND status = 'approved' AND card_id IS NOT NULL
            ORDER BY order_date ASC
        """, (batch_id,))
        return cur.fetchall()


def mark_batch_complete(batch_id: str):
    """Mark all approved rows in batch as processed (status stays 'approved')."""
    with db_cursor() as cur:
        cur.execute("""
            UPDATE staging SET updated_at = NOW()
            WHERE import_batch = %s AND status = 'approved'
        """, (batch_id,))
