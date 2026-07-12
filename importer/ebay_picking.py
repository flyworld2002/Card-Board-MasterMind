"""
importer/ebay_picking.py — snapshot paid-but-unshipped eBay orders into
picking_queue for the Picking tab.

Data source: Sell Fulfillment API getOrders with
    filter=orderfulfillmentstatus:{NOT_STARTED|IN_PROGRESS}
(same OAuth token as --ebay-syncfees; sell.fulfillment scope).

Two server-side gaps the filter does NOT handle (confirmed against eBay's
own docs/KB, not assumed):
  1. Cancelled orders never "start" fulfillment, so they still come back
     from this filter — we skip anything whose cancelState isn't
     NONE_REQUESTED (conservative: an order mid-cancellation shouldn't be
     packed either).
  2. NOT_STARTED includes unpaid orders — we keep only orderPaymentStatus
     in PAID_STATUSES.

picking_queue is a SNAPSHOT, not a source of truth: every run truncates
and rewrites the whole table in one transaction, all accounts together.
Shipped orders drop out automatically on the next run because eBay flips
them to FULFILLED. Shipment grouping / pile badges are frontend concerns —
this module just delivers clean rows.

Line-item ID convention: same composite "{legacyItemId}-{lineItemId}"
rebuild as ebay_syncfees.py, so picking rows can be joined against sales /
sale_line_item_fees if ever needed.

Card matching: same ebay_listing_map lookup as ebay_orders.py — exact
(item_id, variation_name) first, single-variant-listing fallback second.
variation_name for REST lineItems = first variationAspects value, falling
back to the line title (mirrors the Trading API convention: first
VariationSpecifics value, title if no variation).
"""

import os
import requests

from db.connection import db_cursor
from importer.ebay_auth import get_account_name
from importer.ebay_finances import get_access_token, FULFILLMENT_API_BASE

# orderPaymentStatus values we treat as safe to pack.
# PAID is the normal case. PARTIALLY_REFUNDED still means the buyer paid and
# (some of) the order ships — a partial refund on an unshipped order usually
# means one line was refunded, but eBay keeps the unrefunded remainder
# expected to ship. FULLY_REFUNDED / PENDING / FAILED are excluded.
PAID_STATUSES = {"PAID", "PARTIALLY_REFUNDED"}


# ── Account discovery ────────────────────────────────────────────────────────

def detect_accounts() -> list:
    """All account numbers with a refresh token configured in .env
    (EBAY_ACCOUNT_{N}_REFRESH_TOKEN), probing from 1 until a gap."""
    accounts = []
    n = 1
    while os.getenv(f"EBAY_ACCOUNT_{n}_REFRESH_TOKEN"):
        accounts.append(n)
        n += 1
    return accounts


# ── Fetching ─────────────────────────────────────────────────────────────────

def _fetch_unshipped_orders(account_num: int) -> list:
    """Paginates getOrders for all NOT_STARTED / IN_PROGRESS orders."""
    token = get_access_token(account_num)
    orders = []
    offset = 0
    while True:
        resp = requests.get(
            f"{FULFILLMENT_API_BASE}/order",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            params={
                "filter": "orderfulfillmentstatus:{NOT_STARTED|IN_PROGRESS}",
                "limit": 50,
                "offset": offset,
            },
            timeout=25,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"getOrders failed ({resp.status_code}) for account {account_num}: {resp.text[:300]}"
            )
        data = resp.json()
        batch = data.get("orders", [])
        orders.extend(batch)
        if not data.get("next") or not batch:
            break
        offset += len(batch)
    return orders


# ── Per-order → picking rows ─────────────────────────────────────────────────

def _variation_name(line: dict) -> str:
    """First variationAspects value; line title if no variation — mirrors
    the Trading API convention used by ebay_orders.py / the importer."""
    for aspect in line.get("variationAspects", []):
        val = (aspect.get("value") or "").strip()
        if val:
            return val
    return line.get("title", "") or ""


def _match_line(cur, item_id: str, variation_name: str):
    """Same matching rules as ebay_orders.py: exact (item_id, variation_name),
    fallback to item_id alone only when that listing maps to one variant.
    Returns variant_id or None."""
    cur.execute(
        """
        SELECT variant_id FROM ebay_listing_map
        WHERE item_id = %s AND variation_name = %s
        """,
        (item_id, variation_name),
    )
    row = cur.fetchone()
    if row:
        return row["variant_id"]

    cur.execute(
        "SELECT variant_id FROM ebay_listing_map WHERE item_id = %s",
        (item_id,),
    )
    rows = cur.fetchall()
    if len(rows) == 1:
        return rows[0]["variant_id"]
    return None


def _card_info(cur, variant_ids: list) -> dict:
    """variant_id -> {card_name, card_number, set_name, variant_label},
    resolved in one query via v_card_variants."""
    if not variant_ids:
        return {}
    cur.execute(
        """
        SELECT variant_id, card_name,
               coalesce(display_number, card_number) AS card_number,
               set_name,
               foil_label,
               stamp_label, pattern_label
        FROM v_card_variants
        WHERE variant_id = ANY(%s::uuid[])
        """,
        (variant_ids,),
    )
    info = {}
    for row in cur.fetchall():
        # foil_label is the axis that matters for grabbing the right printing;
        # stamp/pattern appended only when present so the label stays short.
        parts = [row.get("foil_label"), row.get("stamp_label"), row.get("pattern_label")]
        label = " · ".join(p for p in parts if p) or "Standard"
        info[row["variant_id"]] = {
            "card_name":   row["card_name"],
            "card_number": row["card_number"],
            "set_name":    row["set_name"],
            "variant_label": label,
        }
    return info


def _order_to_rows(cur, order: dict, account_num: int) -> list:
    """Flattens one Fulfillment order into picking_queue row dicts.
    Returns [] for orders that shouldn't be packed (unpaid / cancelled)."""
    cancel_state = order.get("cancelStatus", {}).get("cancelState", "NONE_REQUESTED")
    if cancel_state != "NONE_REQUESTED":
        return []  # cancelled or mid-cancellation — never pack

    if order.get("orderPaymentStatus") not in PAID_STATUSES:
        return []

    order_id = order.get("orderId")
    fulfillment_status = order.get("orderFulfillmentStatus")

    # paid_at: first payment date; creationDate as fallback
    paid_at = None
    for p in order.get("paymentSummary", {}).get("payments", []):
        if p.get("paymentDate"):
            paid_at = p["paymentDate"]
            break
    paid_at = paid_at or order.get("creationDate")

    buyer = order.get("buyer", {})
    ship_to = {}
    for instr in order.get("fulfillmentStartInstructions", []):
        step = instr.get("shippingStep", {})
        if step.get("shipTo"):
            ship_to = step["shipTo"]
            break
    ship_addr = ship_to.get("contactAddress", {})

    # First pass: build lines + collect variant ids to resolve in one query
    lines = []
    for li in order.get("lineItems", []):
        lid = li.get("lineItemId")
        legacy = li.get("legacyItemId")
        if not lid:
            continue
        var_name = _variation_name(li)
        variant_id = _match_line(cur, legacy, var_name) if legacy else None
        lines.append({
            "line_item_id": f"{legacy}-{lid}" if legacy else lid,
            "ebay_item_id": legacy,
            "title":        li.get("title", ""),
            "var_name":     var_name,
            "quantity":     int(li.get("quantity", 1)),
            "variant_id":   variant_id,
        })

    card_info = _card_info(cur, [l["variant_id"] for l in lines if l["variant_id"]])

    rows = []
    for l in lines:
        info = card_info.get(l["variant_id"], {})
        rows.append({
            "account_num":        account_num,
            "platform_order_id":  order_id,
            "order_line_item_id": l["line_item_id"],
            "ebay_item_id":       l["ebay_item_id"],
            "listing_title":      l["title"],
            "card_name":          info.get("card_name"),
            "card_number":        info.get("card_number"),
            "set_name":           info.get("set_name"),
            "variant_label":      info.get("variant_label"),
            "quantity":           l["quantity"],
            "matched":            bool(info),
            "raw_variation_name": l["var_name"],
            "buyer_username":     buyer.get("username"),
            "ship_name":          ship_to.get("fullName"),
            "ship_city":          ship_addr.get("city"),
            "ship_state":         ship_addr.get("stateOrProvince"),
            "ship_postal_code":   ship_addr.get("postalCode"),
            "ship_country":       ship_addr.get("countryCode"),
            "paid_at":            paid_at,
            "order_fulfillment_status": fulfillment_status,
        })
    return rows


# ── Entry point ──────────────────────────────────────────────────────────────

def pull_picking(account_nums: list = None, dry_run: bool = False,
                 quiet: bool = False) -> dict:
    """
    Snapshot paid-but-unshipped orders (all given accounts, default: every
    account with a refresh token in .env) into picking_queue.

    Truncate + insert happens in ONE transaction AFTER all API calls finish,
    so the table always holds either the previous complete snapshot or the
    new complete snapshot — never a half-written one (the picking API serves
    this table while a refresh may be in flight).

    Returns a summary dict (used by the picking API endpoint):
      {"orders": N, "lines": N, "unmatched": N, "accounts": [..], "dry_run": bool}
    """
    from datetime import datetime

    accounts = account_nums or detect_accounts()
    if not accounts:
        raise EnvironmentError(
            "No accounts with EBAY_ACCOUNT_{N}_REFRESH_TOKEN found in .env — "
            "the picking pull uses the Fulfillment API and needs OAuth."
        )

    def p(msg):
        if not quiet:
            print(msg)

    p(f"\n📦 Pulling unshipped orders for picking — account(s) {accounts}"
      + (" [DRY RUN]" if dry_run else ""))

    # Phase 1: all API calls + matching, no writes. Matching needs a cursor
    # but is read-only; keep it separate from the write transaction.
    all_rows = []
    order_count = 0
    with db_cursor() as cur:
        for account_num in accounts:
            account = get_account_name(account_num)
            orders = _fetch_unshipped_orders(account_num)
            kept = 0
            for order in orders:
                rows = _order_to_rows(cur, order, account_num)
                if rows:
                    kept += 1
                    all_rows.extend(rows)
            order_count += kept
            p(f"   account {account_num} ({account}): {len(orders)} returned, "
              f"{kept} packable")

    unmatched = sum(1 for r in all_rows if not r["matched"])

    if dry_run:
        for r in all_rows:
            desc = (f"{r['card_number']} {r['card_name']} · {r['variant_label']}"
                    if r["matched"] else f"⚠️ UNMATCHED: {r['raw_variation_name'][:50]!r}")
            p(f"   [dry-run] {r['platform_order_id']} · {desc} ×{r['quantity']} "
              f"→ {r['buyer_username']} ({r['ship_city']}, {r['ship_state'] or r['ship_country']})")
    else:
        # Phase 2: atomic snapshot swap.
        with db_cursor() as cur:
            cur.execute("TRUNCATE picking_queue")
            for r in all_rows:
                cur.execute(
                    """
                    INSERT INTO picking_queue (
                        account_num, platform_order_id, order_line_item_id,
                        ebay_item_id, listing_title,
                        card_name, card_number, set_name, variant_label,
                        quantity, matched, raw_variation_name,
                        buyer_username, ship_name, ship_city, ship_state,
                        ship_postal_code, ship_country,
                        paid_at, order_fulfillment_status
                    ) VALUES (
                        %(account_num)s, %(platform_order_id)s, %(order_line_item_id)s,
                        %(ebay_item_id)s, %(listing_title)s,
                        %(card_name)s, %(card_number)s, %(set_name)s, %(variant_label)s,
                        %(quantity)s, %(matched)s, %(raw_variation_name)s,
                        %(buyer_username)s, %(ship_name)s, %(ship_city)s, %(ship_state)s,
                        %(ship_postal_code)s, %(ship_country)s,
                        %(paid_at)s, %(order_fulfillment_status)s
                    )
                    """,
                    r,
                )

    summary = {
        "orders": order_count,
        "lines": len(all_rows),
        "unmatched": unmatched,
        "accounts": accounts,
        "dry_run": dry_run,
        "pulled_at": datetime.utcnow().isoformat() + "Z",
    }

    if quiet:
        from datetime import timezone
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{ts}] picking accounts {accounts}{' [DRY RUN]' if dry_run else ''}: "
              f"orders={order_count} lines={len(all_rows)} unmatched={unmatched}")
    else:
        p(f"\n📊 Snapshot: {order_count} order(s), {len(all_rows)} line(s), "
          f"{unmatched} unmatched"
          + (" (dry run — nothing was written)" if dry_run else ""))

    return summary
