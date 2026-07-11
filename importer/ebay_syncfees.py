"""
importer/ebay_syncfees.py — populate sale_orders / sale_line_item_fees from
eBay's Finances API (getTransactions) and Fulfillment API (getOrder).

Field mapping — confirmed against real orders, not guessed from docs:

  sale_orders (order-level; no per-line breakdown exists for these):
    label_cost           <- Finances SHIPPING_LABEL txn, no RETURN_ID reference
    return_label_cost    <- Finances SHIPPING_LABEL txn, WITH a RETURN_ID reference
    promo_fee             <- Finances NON_SALE_CHARGE / feeType=AD_FEE, summed
    final_value_fee_fixed <- Finances SALE txn, feeType=FINAL_VALUE_FEE_FIXED_PER_ORDER
                             (eBay attaches this to one arbitrary line; we pull
                             it out to the order level on purpose)
    sales_tax             <- Finances SALE txn, ebayCollectedTaxAmount
    promo_discount        <- Fulfillment pricingSummary.priceDiscount
    shipping_charged      <- Fulfillment pricingSummary.deliveryCost + deliveryDiscount
                             (what the buyer actually paid, after any free-shipping promo)
    refund_amount         <- catch-all: net amount of any Finances REFUND txn that
                             has NO orderLineItems[] breakdown (untested path —
                             every real refund we've seen DID have a breakdown)
    buyer_*, ship_*       <- Fulfillment buyer / fulfillmentStartInstructions.shipTo

  sale_line_item_fees (per order_line_item_id; eBay reports these per card):
    final_value_fee   <- Finances SALE txn, sum of marketplaceFees where
                         feeType IN ('FINAL_VALUE_FEE', 'INTERNATIONAL_FEE') for that line
    discount_amount   <- Fulfillment lineItem.appliedPromotions[], summed
    refund_amount     <- NET refund for that line: for each Finances REFUND txn,
                         per orderLineItems[] entry: feeBasisAmount - sum(marketplaceFees).
                         This is money that actually left your payout — NOT the
                         gross amount the buyer received (that's already reflected
                         by the original sale's final_value_fee never being credited).

Two API calls per order (Finances + Fulfillment) — they cover genuinely
different data, eBay does not expose one endpoint with everything.
"""

import time
import requests

from db.connection import db_cursor
from importer.ebay_auth import get_account_name
from importer.ebay_finances import (
    get_access_token,
    FINANCES_API_BASE,
    FULFILLMENT_API_BASE,
)


# ── Fetching ─────────────────────────────────────────────────────────────────

def _fetch_all_transactions(order_id: str, account_num: int) -> list:
    """Paginates through getTransactions for one order. Small pages expected —
    an order rarely has more than a handful of transactions."""
    token = get_access_token(account_num)
    all_tx = []
    offset = 0
    while True:
        resp = requests.get(
            f"{FINANCES_API_BASE}/transaction",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            params={"filter": f"orderId:{{{order_id}}}", "limit": 50, "offset": offset},
            timeout=20,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"getTransactions failed ({resp.status_code}) for {order_id}: {resp.text[:300]}")
        data = resp.json()
        batch = data.get("transactions", [])
        all_tx.extend(batch)
        if not data.get("next") or not batch:
            break
        offset += len(batch)
    return all_tx


def _fetch_order(order_id: str, account_num: int) -> dict:
    token = get_access_token(account_num)
    resp = requests.get(
        f"{FULFILLMENT_API_BASE}/order/{order_id}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        params={"fieldGroups": "TAX_BREAKDOWN"},
        timeout=20,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"getOrder failed ({resp.status_code}) for {order_id}: {resp.text[:300]}")
    return resp.json()


# ── Aggregation ──────────────────────────────────────────────────────────────

def _aggregate(transactions: list, order: dict) -> tuple:
    """Returns (order_fields: dict, line_fields: dict[line_item_id -> dict]).

    IMPORTANT: eBay's REST APIs (Finances/Fulfillment) report lineItemId as a
    bare transaction id (e.g. "10081979031425"). eBay's Trading API — which
    ebay_orders.py uses to populate sales.order_line_item_id — reports the
    same thing as a composite "{ItemID}-{TransactionID}" string (e.g.
    "335776337446-10081979031425"). These are NOT the same string despite
    referring to the same line item, so every lookup keyed on the bare REST
    id would silently fail to match sales rows. We rebuild the composite
    form here using legacyItemId (present per line in the Fulfillment
    response) so sale_line_item_fees keys line up with what's actually in
    the sales table.
    """
    # lineItemId -> "{legacyItemId}-{lineItemId}", built from Fulfillment's
    # lineItems[] (each entry carries its own legacyItemId).
    id_map = {}
    for li in order.get("lineItems", []):
        lid = li.get("lineItemId")
        legacy = li.get("legacyItemId")
        if lid and legacy:
            id_map[lid] = f"{legacy}-{lid}"

    def _composite(lid):
        return id_map.get(lid, lid)  # fall back to bare id if unmapped (shouldn't happen)

    order_fields = {
        "label_cost": 0.0, "return_label_cost": 0.0, "promo_fee": 0.0,
        "final_value_fee_fixed": 0.0, "sales_tax": 0.0,
        "refund_amount": 0.0, "refunded_at": None,
    }
    line_fields = {}  # composite_line_id -> {final_value_fee, refund_amount, refunded_at}

    def _line(lid):
        return line_fields.setdefault(_composite(lid), {"final_value_fee": 0.0, "refund_amount": 0.0, "refunded_at": None})

    for tx in transactions:
        tx_type = tx.get("transactionType")
        amount = float(tx.get("amount", {}).get("value", 0))
        refs = {r.get("referenceType"): r.get("referenceId") for r in tx.get("references", [])}

        if tx_type == "SHIPPING_LABEL":
            if "RETURN_ID" in refs:
                order_fields["return_label_cost"] += amount
            else:
                order_fields["label_cost"] += amount

        elif tx_type == "NON_SALE_CHARGE" and tx.get("feeType") == "AD_FEE":
            order_fields["promo_fee"] += amount

        elif tx_type == "SALE":
            tax = tx.get("ebayCollectedTaxAmount", {}).get("value")
            if tax is not None:
                order_fields["sales_tax"] += float(tax)
            for li in tx.get("orderLineItems", []):
                lid = li.get("lineItemId")
                if not lid:
                    continue
                for fee in li.get("marketplaceFees", []):
                    ftype = fee.get("feeType")
                    famt = float(fee.get("amount", {}).get("value", 0))
                    if ftype == "FINAL_VALUE_FEE_FIXED_PER_ORDER":
                        order_fields["final_value_fee_fixed"] += famt
                    elif ftype in ("FINAL_VALUE_FEE", "INTERNATIONAL_FEE"):
                        _line(lid)["final_value_fee"] += famt

        elif tx_type == "REFUND":
            tx_date = tx.get("transactionDate")
            line_items = tx.get("orderLineItems", [])
            if line_items:
                for li in line_items:
                    lid = li.get("lineItemId")
                    if not lid:
                        continue
                    basis = float(li.get("feeBasisAmount", {}).get("value", 0))
                    fees = sum(float(f.get("amount", {}).get("value", 0)) for f in li.get("marketplaceFees", []))
                    net = basis - fees
                    entry = _line(lid)
                    entry["refund_amount"] += net
                    if not entry["refunded_at"] or tx_date > entry["refunded_at"]:
                        entry["refunded_at"] = tx_date
            else:
                # No line-item breakdown at all — order-level catch-all.
                order_fields["refund_amount"] += amount
                if not order_fields["refunded_at"] or tx_date > order_fields["refunded_at"]:
                    order_fields["refunded_at"] = tx_date

    # Fulfillment side: pricing summary + per-line discount + buyer/ship-to
    ps = order.get("pricingSummary", {})
    delivery_cost = float(ps.get("deliveryCost", {}).get("value", 0))
    delivery_discount = float(ps.get("deliveryDiscount", {}).get("value", 0))
    order_fields["shipping_charged"] = delivery_cost + delivery_discount
    order_fields["promo_discount"] = float(ps.get("priceDiscount", {}).get("value", 0))

    for li in order.get("lineItems", []):
        lid = li.get("lineItemId")
        if not lid:
            continue
        discount = sum(
            float(p.get("discountAmount", {}).get("value", 0))
            for p in li.get("appliedPromotions", [])
        )
        _line(lid)["discount_amount"] = discount

    buyer = order.get("buyer", {})
    ship_to = {}
    for instr in order.get("fulfillmentStartInstructions", []):
        step = instr.get("shippingStep", {})
        if step.get("shipTo"):
            ship_to = step["shipTo"]
            break
    ship_addr = ship_to.get("contactAddress", {})

    order_fields["buyer_username"]     = buyer.get("username")
    order_fields["buyer_full_name"]    = ship_to.get("fullName")
    order_fields["ship_address_line1"] = ship_addr.get("addressLine1")
    order_fields["ship_city"]          = ship_addr.get("city")
    order_fields["ship_state"]         = ship_addr.get("stateOrProvince")
    order_fields["ship_postal_code"]   = ship_addr.get("postalCode")
    order_fields["ship_country"]       = ship_addr.get("countryCode")
    order_fields["buyer_phone"]        = ship_to.get("primaryPhone", {}).get("phoneNumber")
    order_fields["buyer_email"]        = buyer.get("buyerRegistrationAddress", {}).get("email")

    return order_fields, line_fields


# ── DB write ─────────────────────────────────────────────────────────────────

def _upsert_order(cur, platform, account, order_id, f, dry_run):
    if dry_run:
        print(f"   [dry-run] would upsert sale_orders for {order_id}: "
              f"label={f['label_cost']:.2f} return_label={f['return_label_cost']:.2f} "
              f"promo_fee={f['promo_fee']:.2f} fvf_fixed={f['final_value_fee_fixed']:.2f} "
              f"tax={f['sales_tax']:.2f} discount={f['promo_discount']:.2f} "
              f"shipping={f['shipping_charged']:.2f} refund={f['refund_amount']:.2f} "
              f"buyer={f.get('buyer_username')}")
        return

    cur.execute(
        """
        INSERT INTO sale_orders (
            platform, account, platform_order_id,
            label_cost, return_label_cost, promo_fee, final_value_fee_fixed,
            sales_tax, promo_discount, shipping_charged,
            refund_amount, refunded_at,
            buyer_username, buyer_full_name, ship_address_line1, ship_city,
            ship_state, ship_postal_code, ship_country, buyer_phone, buyer_email,
            fees_synced_at
        ) VALUES (
            %(platform)s, %(account)s, %(order_id)s,
            %(label_cost)s, %(return_label_cost)s, %(promo_fee)s, %(final_value_fee_fixed)s,
            %(sales_tax)s, %(promo_discount)s, %(shipping_charged)s,
            %(refund_amount)s, %(refunded_at)s,
            %(buyer_username)s, %(buyer_full_name)s, %(ship_address_line1)s, %(ship_city)s,
            %(ship_state)s, %(ship_postal_code)s, %(ship_country)s, %(buyer_phone)s, %(buyer_email)s,
            now()
        )
        ON CONFLICT (platform, coalesce(account, ''), platform_order_id) DO UPDATE SET
            label_cost = EXCLUDED.label_cost,
            return_label_cost = EXCLUDED.return_label_cost,
            promo_fee = EXCLUDED.promo_fee,
            final_value_fee_fixed = EXCLUDED.final_value_fee_fixed,
            sales_tax = EXCLUDED.sales_tax,
            promo_discount = EXCLUDED.promo_discount,
            shipping_charged = EXCLUDED.shipping_charged,
            refund_amount = EXCLUDED.refund_amount,
            refunded_at = EXCLUDED.refunded_at,
            buyer_username = EXCLUDED.buyer_username,
            buyer_full_name = EXCLUDED.buyer_full_name,
            ship_address_line1 = EXCLUDED.ship_address_line1,
            ship_city = EXCLUDED.ship_city,
            ship_state = EXCLUDED.ship_state,
            ship_postal_code = EXCLUDED.ship_postal_code,
            ship_country = EXCLUDED.ship_country,
            buyer_phone = EXCLUDED.buyer_phone,
            buyer_email = EXCLUDED.buyer_email,
            fees_synced_at = now()
        """,
        {
            "platform": platform, "account": account, "order_id": order_id,
            "label_cost": f["label_cost"], "return_label_cost": f["return_label_cost"],
            "promo_fee": f["promo_fee"], "final_value_fee_fixed": f["final_value_fee_fixed"],
            "sales_tax": f["sales_tax"], "promo_discount": f["promo_discount"],
            "shipping_charged": f["shipping_charged"],
            "refund_amount": f["refund_amount"] or None, "refunded_at": f["refunded_at"],
            "buyer_username": f.get("buyer_username"), "buyer_full_name": f.get("buyer_full_name"),
            "ship_address_line1": f.get("ship_address_line1"), "ship_city": f.get("ship_city"),
            "ship_state": f.get("ship_state"), "ship_postal_code": f.get("ship_postal_code"),
            "ship_country": f.get("ship_country"), "buyer_phone": f.get("buyer_phone"),
            "buyer_email": f.get("buyer_email"),
        },
    )


def _upsert_line_items(cur, platform, account, line_fields, dry_run):
    for lid, f in line_fields.items():
        if dry_run:
            print(f"      [dry-run] would upsert sale_line_item_fees for {lid}: "
                  f"fvf={f.get('final_value_fee', 0):.2f} "
                  f"discount={f.get('discount_amount', 0):.2f} "
                  f"refund={f.get('refund_amount', 0):.2f}")
            continue
        cur.execute(
            """
            INSERT INTO sale_line_item_fees (
                platform, account, order_line_item_id,
                final_value_fee, discount_amount, refund_amount, refunded_at, synced_at
            ) VALUES (
                %(platform)s, %(account)s, %(lid)s,
                %(final_value_fee)s, %(discount_amount)s, %(refund_amount)s, %(refunded_at)s, now()
            )
            ON CONFLICT (platform, coalesce(account, ''), order_line_item_id) DO UPDATE SET
                final_value_fee = EXCLUDED.final_value_fee,
                discount_amount = EXCLUDED.discount_amount,
                refund_amount = EXCLUDED.refund_amount,
                refunded_at = EXCLUDED.refunded_at,
                synced_at = now()
            """,
            {
                "platform": platform, "account": account, "lid": lid,
                "final_value_fee": f.get("final_value_fee") or None,
                "discount_amount": f.get("discount_amount") or None,
                "refund_amount": f.get("refund_amount") or None,
                "refunded_at": f.get("refunded_at"),
            },
        )


# ── Order selection ──────────────────────────────────────────────────────────

def _orders_to_sync(cur, account: str, since_str: str, until_str: str, order_ids: list) -> list:
    if order_ids:
        return order_ids

    query = """
        SELECT DISTINCT platform_order_id
        FROM sales
        WHERE platform = 'ebay'
          AND account = %s
          AND order_line_item_id IS NOT NULL
          AND platform_order_id IS NOT NULL
    """
    params = [account]
    if since_str:
        query += " AND sold_at >= %s"
        params.append(since_str)
    if until_str:
        query += " AND sold_at <= %s"
        params.append(until_str)

    cur.execute(query, params)
    return [row["platform_order_id"] for row in cur.fetchall()]


# ── Entry point ──────────────────────────────────────────────────────────────

def set_label_cost(order_id: str, amount: float, account_num: int = 1,
                    is_return_label: bool = False, dry_run: bool = False) -> None:
    """
    Manual override for label_cost / return_label_cost on a specific order,
    for cases where eBay's Finances API never posts a SHIPPING_LABEL
    transaction even though Seller Hub clearly shows one (a real, observed
    gap — see project notes). Also the intended path for international
    Pirate Ship labels, which the Finances API cannot see at all by design.

    Only touches the one field requested — never overwrites other synced
    data on the row. Requires the sale_orders row to already exist (i.e.
    --ebay-syncfees must have run at least once for this order already).
    """
    account = get_account_name(account_num)
    field = "return_label_cost" if is_return_label else "label_cost"

    print(f"\n✏️  Manual {field} override — order {order_id}, account {account_num} ({account})"
          + (" [DRY RUN]" if dry_run else ""))

    if dry_run:
        print(f"   [dry-run] would set {field} = {amount:.2f}")
        return

    with db_cursor() as cur:
        cur.execute(
            f"""
            UPDATE sale_orders
            SET {field} = %(amount)s
            WHERE platform = 'ebay'
              AND coalesce(account, '') = coalesce(%(account)s, '')
              AND platform_order_id = %(order_id)s
            """,
            {"amount": amount, "account": account, "order_id": order_id},
        )
        if cur.rowcount == 0:
            print(f"   ⚠️  No sale_orders row found for {order_id} — run --ebay-syncfees "
                  f"for this order first (even if it can't find the label, it needs to "
                  f"create the header row before a manual override has anywhere to land).")
        else:
            print(f"   ✅ {field} set to {amount:.2f}")


def sync_fees(account_num: int = 1, since_str: str = None, until_str: str = None,
              order_ids: list = None, dry_run: bool = False, sleep_between: float = 0.2) -> None:
    account = get_account_name(account_num)
    print(f"\n💰 Syncing eBay fees — account {account_num} ({account})"
          + (" [DRY RUN]" if dry_run else ""))

    if order_ids:
        targets = order_ids  # explicit list — no DB lookup needed
    else:
        with db_cursor() as cur:
            targets = _orders_to_sync(cur, account, since_str, until_str, order_ids)
    print(f"   {len(targets)} order(s) to sync\n")

    synced = errors = 0
    for order_id in targets:
        try:
            # API calls happen OUTSIDE any DB transaction — these are slow
            # network round-trips and shouldn't hold a lock open while we wait.
            transactions = _fetch_all_transactions(order_id, account_num)
            order = _fetch_order(order_id, account_num)
            order_fields, line_fields = _aggregate(transactions, order)

            print(f"   {order_id}: {len(transactions)} txn(s), {len(line_fields)} line(s)")

            if dry_run:
                # Dry run never touches the DB at all — pure API + aggregation preview.
                _upsert_order(cur=None, platform="ebay", account=account, order_id=order_id,
                              f=order_fields, dry_run=True)
                _upsert_line_items(cur=None, platform="ebay", account=account,
                                    line_fields=line_fields, dry_run=True)
            else:
                # Each order gets its own short-lived transaction, so one bad
                # order can't roll back everything already synced before it.
                with db_cursor() as cur:
                    _upsert_order(cur, "ebay", account, order_id, order_fields, dry_run)
                    _upsert_line_items(cur, "ebay", account, line_fields, dry_run)
            synced += 1
        except Exception as e:
            print(f"   ⚠️  {order_id}: {e}")
            errors += 1

        time.sleep(sleep_between)  # gentle on rate limits across many orders

    print(f"\n📊 Synced: {synced} | Errors: {errors}"
          + (" (dry run — nothing was written)" if dry_run else ""))
