"""
importer/market_price_refresh.py
Refreshes market_prices from the Pokemon TCG API, scoped to one set
(by name) or one card (by card_master.id). See
docs/plans/listing-pricing-system.md.

One Pokemon TCG API card lookup (get_card_by_id) returns pricing for
ALL of that card's foil-type variants (normal/holofoil/reverseHolofoil)
in a single response, so the unit of work here is one card_master row
(one external_id), not one card_variants row — a card with a holo +
reverse-holo variant costs one API call, not two.

For a set-scoped refresh, a card is skipped if every one of its variants
already has a market_prices row (condition='Near Mint') updated today —
re-running a set's refresh only re-hits what's actually stale. A single
explicit --card-id refresh always calls the API regardless of freshness,
since that's a deliberate "do it now" click, not a batch sweep.

Concurrency: up to MAX_WORKERS card lookups in flight via
ThreadPoolExecutor. db_cursor() opens its own connection per call, so
concurrent upserts from different worker threads need no extra locking.
A single card's API failure (network error, unmapped external_id, no
price data) is counted as failed and does not abort the rest of the
batch — re-running the set refresh is itself the retry mechanism, since
only the still-stale cards get re-attempted.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed

from db.connection import db_cursor, upsert_market_price
from utils.pokemon_api import get_card_by_id, extract_market_price

MAX_WORKERS = 15
CONDITION = "Near Mint"


def _cards_needing_refresh(set_name: str = None, card_id: str = None) -> list[dict]:
    """Returns [{card_master_id, external_id, variants: [{variant_id, foil_type}, ...]}, ...]."""
    with db_cursor() as cur:
        if card_id:
            cur.execute("""
                SELECT cm.id AS card_master_id, cm.external_id
                FROM card_master cm
                WHERE cm.id = %s AND cm.external_id IS NOT NULL
            """, (card_id,))
        else:
            cur.execute("""
                SELECT cm.id AS card_master_id, cm.external_id
                FROM card_master cm
                JOIN card_sets cs ON cm.set_id = cs.id
                WHERE cs.name = %s AND cm.external_id IS NOT NULL
                  AND EXISTS (
                    SELECT 1 FROM card_variants cv
                    WHERE cv.card_id = cm.id
                      AND NOT EXISTS (
                        SELECT 1 FROM market_prices mp
                        WHERE mp.variant_id = cv.id AND mp.condition = %s
                          AND mp.updated_at::date = CURRENT_DATE
                      )
                  )
            """, (set_name, CONDITION))
        cards = cur.fetchall()
        if not cards:
            return []

        card_ids = [c["card_master_id"] for c in cards]
        cur.execute("""
            SELECT id AS variant_id, card_id, foil_type
            FROM card_variants
            WHERE card_id = ANY(%s::uuid[])
        """, (card_ids,))
        variants_by_card: dict[str, list[dict]] = {}
        for v in cur.fetchall():
            variants_by_card.setdefault(str(v["card_id"]), []).append(
                {"variant_id": str(v["variant_id"]), "foil_type": v["foil_type"]}
            )

    return [
        {
            "card_master_id": str(c["card_master_id"]),
            "external_id": c["external_id"],
            "variants": variants_by_card.get(str(c["card_master_id"]), []),
        }
        for c in cards
    ]


def _refresh_one_card(card: dict) -> dict:
    """One API call + one upsert per variant. Never raises — a per-card
    failure is reported back as {"ok": False} instead of aborting the
    whole ThreadPoolExecutor batch."""
    try:
        api_card = get_card_by_id(card["external_id"])
        if not api_card:
            return {"card_master_id": card["card_master_id"], "ok": False,
                     "error": "not found in API", "variants_updated": 0}

        updated = 0
        for v in card["variants"]:
            price, _date = extract_market_price(api_card, v["foil_type"])
            if price is not None:
                upsert_market_price(v["variant_id"], CONDITION, price, source="tcgplayer")
                updated += 1
        return {"card_master_id": card["card_master_id"], "ok": True, "variants_updated": updated}
    except Exception as e:
        return {"card_master_id": card["card_master_id"], "ok": False,
                 "error": str(e), "variants_updated": 0}


def refresh_market_prices(job_id: str = None, set_name: str = None, card_id: str = None,
                           dry_run: bool = False) -> dict:
    """
    Entry point for both the CLI (--refresh-market-prices) and the
    picking_api.py background job (POST /api/jobs/market-price-refresh).
    Exactly one of set_name/card_id must be given. Streams progress into
    job_runner.update_job() when job_id is given, so the Jobs page can
    poll live counts; the CLI path (job_id=None) just returns the final
    summary.
    """
    if not set_name and not card_id:
        raise ValueError("refresh_market_prices requires set_name or card_id")

    cards = _cards_needing_refresh(set_name=set_name, card_id=card_id)
    total = len(cards)

    if job_id:
        from importer.job_runner import update_job
        update_job(job_id, total=total, done=0, failed=0, variants_updated=0)

    if dry_run:
        return {"total": total, "done": 0, "failed": 0, "variants_updated": 0,
                 "cards": [c["card_master_id"] for c in cards]}
    if not cards:
        return {"total": 0, "done": 0, "failed": 0, "variants_updated": 0}

    done = failed = variants_updated = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [pool.submit(_refresh_one_card, c) for c in cards]
        for fut in as_completed(futures):
            outcome = fut.result()
            done += 1
            if outcome["ok"]:
                variants_updated += outcome["variants_updated"]
            else:
                failed += 1
            if job_id:
                from importer.job_runner import update_job
                update_job(job_id, total=total, done=done, failed=failed, variants_updated=variants_updated)

    return {"total": total, "done": done, "failed": failed, "variants_updated": variants_updated}
