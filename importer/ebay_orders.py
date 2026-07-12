"""
importer/ebay_orders.py — Pull eBay orders and record sales

Fetches paid orders via the Trading API (GetOrders), matches each order
line to a variant through ebay_listing_map (item_id + variation_name),
and records the sale through the record_sale RPC (FIFO depletion,
quantity_sold bump — all atomic per line).

Also decrements platform_listings.quantity_listed for the matched
listing, and files unmatched / insufficient-stock lines into
ebay_order_issues so they survive past the pull window. Open issues
are retried automatically at the start of every run.

Dedup is by eBay's OrderLineItemID against sales.order_line_item_id
(platform_order_id holds eBay's order-level OrderID for grouping),
so the command is safe to re-run over any window.

Usage:
    python main.py --ebay-pullorders
    python main.py --ebay-pullorders --dry-run
    python main.py --ebay-pullorders --since 2026-07-01
    python main.py --ebay-pullorders --since 2026-07-01 --until 2026-07-03
    python main.py --ebay-pullorders --order 12-34567-89012
    python main.py --ebay-pullorders --account 2
"""

import json
from datetime import datetime, timezone

from importer.ebay import _post, _find, _findall, _text
from importer.ebay_auth import get_user_token, get_account_name
from db.connection import db_cursor


# ══════════════════════════════════════════════════════════════════════════════
# Sync-state helpers
# ══════════════════════════════════════════════════════════════════════════════

def _state_key(account_num: int) -> str:
    return f"ebay_orders_last_pull_account_{account_num}"


def _get_last_pull(cur, account_num: int) -> datetime | None:
    cur.execute("SELECT value FROM sync_state WHERE key = %s", (_state_key(account_num),))
    row = cur.fetchone()
    if row and row["value"]:
        return datetime.fromisoformat(row["value"])
    return None


def _set_last_pull(cur, account_num: int, ts: datetime):
    cur.execute(
        """
        INSERT INTO sync_state (key, value, updated_at)
        VALUES (%s, %s, now())
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
        """,
        (_state_key(account_num), ts.isoformat()),
    )


def _paid_floor_key(account_num: int) -> str:
    return f"ebay_orders_paid_floor_account_{account_num}"


def _get_paid_floor(cur, account_num: int) -> datetime | None:
    """
    Persistent floor on PaidTime — any order line paid before this gets
    diverted to a 'pre_inventory' issue instead of being recorded, so a
    sale that predates your inventory snapshot can never accidentally
    deplete stock it has no relationship to. Set once via --paid-since;
    sticks for every future run until changed.
    """
    cur.execute("SELECT value FROM sync_state WHERE key = %s", (_paid_floor_key(account_num),))
    row = cur.fetchone()
    if row and row["value"]:
        return datetime.fromisoformat(row["value"])
    return None


def _set_paid_floor(cur, account_num: int, ts: datetime):
    cur.execute(
        """
        INSERT INTO sync_state (key, value, updated_at)
        VALUES (%s, %s, now())
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
        """,
        (_paid_floor_key(account_num), ts.isoformat()),
    )


# ══════════════════════════════════════════════════════════════════════════════
# Step 1 — Fetch paid orders via GetOrders (paginated)
# ══════════════════════════════════════════════════════════════════════════════

def fetch_orders(since: datetime = None, until: datetime = None,
                 order_ids: list[str] = None, account_num: int = 1) -> list[dict]:
    """
    Returns one dict per order LINE (transaction).

    Two modes:
      - Time window: ModTimeFrom = since (+ optional ModTimeTo = until).
        ModTime, not CreateTime, so orders paid late still get picked up.
        eBay caps the window at 30 days.
      - Specific orders: order_ids given -> OrderIDArray, no time window.
    """
    lines = []
    page = 1

    while True:
        if order_ids:
            id_xml = "".join(f"<OrderID>{oid}</OrderID>" for oid in order_ids)
            selector = f"<OrderIDArray>{id_xml}</OrderIDArray>"
        else:
            selector = f"<ModTimeFrom>{since.strftime('%Y-%m-%dT%H:%M:%S.000Z')}</ModTimeFrom>"
            if until:
                selector += f"\n  <ModTimeTo>{until.strftime('%Y-%m-%dT%H:%M:%S.000Z')}</ModTimeTo>"
            selector += "\n  <OrderStatus>Completed</OrderStatus>"

        xml_body = f"""<?xml version="1.0" encoding="utf-8"?>
<GetOrdersRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials>
    <eBayAuthToken>{get_user_token(account_num)}</eBayAuthToken>
  </RequesterCredentials>
  {selector}
  <OrderRole>Seller</OrderRole>
  <DetailLevel>ReturnAll</DetailLevel>
  <Pagination>
    <EntriesPerPage>100</EntriesPerPage>
    <PageNumber>{page}</PageNumber>
  </Pagination>
</GetOrdersRequest>"""

        root = _post("GetOrders", xml_body, account_num=account_num)

        order_array = _find(root, "OrderArray")
        orders = _findall(order_array, "Order") if order_array is not None else []

        for order in orders:
            order_id  = _text(order, "OrderID")
            paid_time = _text(order, "PaidTime")
            if not paid_time:
                continue  # unpaid — skip; will reappear once paid (ModTime bumps)

            checkout = _find(order, "CheckoutStatus")
            if checkout is not None and _text(checkout, "Status") != "Complete":
                continue

            # Cancellation: Trading API returns this at the ORDER level (not
            # per-line). "NotApplicable" is the normal/no-cancellation value —
            # anything else means a cancellation was requested/closed against
            # this order. We don't enumerate every possible non-"NotApplicable"
            # value (CancelRequested, CancelClosed, etc.) — any of them means
            # "don't treat this as a normal sale," so _process_line handles
            # all non-NotApplicable values the same way.
            cancel_status = _text(order, "CancelStatus", "NotApplicable")

            tx_array = _find(order, "TransactionArray")
            if tx_array is None:
                continue

            for tx in _findall(tx_array, "Transaction"):
                item = _find(tx, "Item")
                item_id = _text(item, "ItemID") if item is not None else None
                title   = _text(item, "Title", "") if item is not None else ""

                # Same variation_name convention as the importer:
                # first VariationSpecifics value; listing title if no variation.
                var_name = None
                variation = _find(tx, "Variation")
                if variation is not None:
                    specifics = _find(variation, "VariationSpecifics")
                    if specifics is not None:
                        for nvl in _findall(specifics, "NameValueList"):
                            val_el = _find(nvl, "Value")
                            if val_el is not None and val_el.text:
                                var_name = val_el.text.strip()
                                break
                if not var_name:
                    var_name = title

                price_el = _find(tx, "TransactionPrice")
                price = float(price_el.text) if price_el is not None and price_el.text else 0.0

                lines.append({
                    "order_id":           order_id,
                    "order_line_item_id": _text(tx, "OrderLineItemID"),
                    "item_id":            item_id,
                    "variation_name":     var_name or "",
                    "title":              title,
                    "quantity":           int(_text(tx, "QuantityPurchased", "1")),
                    "sale_price":         price,
                    "paid_at":            paid_time,
                    "cancel_status":      cancel_status,
                })

        has_more = (_text(root, "HasMoreOrders", "false") == "true")
        if not has_more:
            break
        page += 1

    return lines


# ══════════════════════════════════════════════════════════════════════════════
# Step 2 — Match a line to (variant_id, condition) via ebay_listing_map
# ══════════════════════════════════════════════════════════════════════════════

def _describe_variant(cur, variant_id: str) -> str:
    """Look up a human-readable card description for a variant_id (for dry-run display)."""
    cur.execute(
        """
        SELECT cm.name AS card_name, cm.card_number, cs.name AS set_name,
               cv.foil_type, cv.foil_pattern, cv.texture, cv.material,
               cv.size, cv.stamp_type, cv.source_type
        FROM card_variants cv
        JOIN card_master cm ON cv.card_id = cm.id
        JOIN card_sets cs   ON cm.set_id = cs.id
        WHERE cv.id = %s
        """,
        (variant_id,),
    )
    row = cur.fetchone()
    if not row:
        return f"(variant {variant_id} not found)"

    axes = [row.get(k) for k in ("foil_type", "foil_pattern", "texture",
                                  "material", "size", "stamp_type", "source_type")]
    variant_str = " · ".join(a for a in axes if a) or "Standard"
    return f"{row['card_name']} #{row['card_number']} ({row['set_name']}) — {variant_str}"


def _match_line(cur, line: dict):
    """
    Returns (variant_id, condition) or (None, reason_detail).
    Exact match on (item_id, variation_name); falls back to item_id
    alone only when that listing maps to exactly one variant.
    """
    cur.execute(
        """
        SELECT variant_id, condition FROM ebay_listing_map
        WHERE item_id = %s AND variation_name = %s
        """,
        (line["item_id"], line["variation_name"]),
    )
    row = cur.fetchone()
    if row:
        return row["variant_id"], row["condition"]

    cur.execute(
        "SELECT variant_id, condition FROM ebay_listing_map WHERE item_id = %s",
        (line["item_id"],),
    )
    rows = cur.fetchall()
    if len(rows) == 1:
        return rows[0]["variant_id"], rows[0]["condition"]

    detail = (
        f"no map entry for item {line['item_id']}"
        if len(rows) == 0
        else f"item {line['item_id']} has {len(rows)} variations; none named {line['variation_name']!r}"
    )
    return None, detail


# ══════════════════════════════════════════════════════════════════════════════
# Step 3 — Record one line (dedup → match → record_sale → listing decrement)
# ══════════════════════════════════════════════════════════════════════════════

def _process_line(cur, line: dict, account: str, dry_run: bool,
                  paid_floor: datetime = None) -> str:
    """Returns: recorded | recorded_with_gap | duplicate | unmatched |
                insufficient_stock | pre_inventory | cancelled |
                cancelled_after_recording | error"""

    # Paid-date floor: a sale that happened before your inventory snapshot
    # existed has no valid relationship to current stock — never attempt
    # record_sale for it, regardless of what the pull window (ModTime)
    # swept in. File it separately for manual review instead.
    if paid_floor and line.get("paid_at"):
        paid_dt = line["paid_at"]
        if isinstance(paid_dt, str):
            paid_dt = datetime.fromisoformat(paid_dt.replace("Z", "+00:00"))
        if paid_dt < paid_floor:
            _file_issue(
                cur, line, account, "pre_inventory",
                f"paid {paid_dt.isoformat()} — before paid-floor cutoff "
                f"{paid_floor.isoformat()}; skipped to avoid depleting "
                f"unrelated current stock",
                dry_run,
            )
            return "pre_inventory"

    # Dedup on OrderLineItemID
    cur.execute(
        "SELECT 1 FROM sales WHERE platform = 'ebay' AND order_line_item_id = %s LIMIT 1",
        (line["order_line_item_id"],),
    )
    already_recorded = bool(cur.fetchone())

    # Cancellation — checked BEFORE match/record_sale. Filed as a visible,
    # non-auto-retried issue either way (Fei's call: keep it in the Issues
    # list for manual review until the pattern's trusted enough to automate).
    # Two distinct cases, since they carry very different stakes:
    #   - never recorded: informational only, nothing to undo
    #   - already recorded: inventory was decremented for a sale that no
    #     longer exists — needs a human decision on whether/how to reverse it
    if line.get("cancel_status", "NotApplicable") != "NotApplicable":
        if already_recorded:
            _file_issue(
                cur, line, account, "cancelled_after_recording",
                f"order cancelled on eBay (CancelStatus={line['cancel_status']}) "
                f"AFTER this line was already recorded as a sale — inventory was "
                f"decremented for a sale that no longer exists. Review and reverse "
                f"manually if appropriate (Sales tab).",
                dry_run,
            )
            return "cancelled_after_recording"
        else:
            _file_issue(
                cur, line, account, "cancelled",
                f"order cancelled on eBay (CancelStatus={line['cancel_status']}) "
                f"before ever being recorded — no inventory impact, informational only.",
                dry_run,
            )
            return "cancelled"

    if already_recorded:
        return "duplicate"

    variant_id, cond_or_detail = _match_line(cur, line)
    if variant_id is None:
        _file_issue(cur, line, account, "unmatched", cond_or_detail, dry_run)
        return "unmatched"
    condition = cond_or_detail
    line["_card_desc"] = _describe_variant(cur, variant_id)

    if dry_run:
        print(f"    [dry-run] would record: {line['title'][:50]}")
        print(f"              → order {line['order_id']} / item {line['item_id']} / {line['variation_name']!r}")
        print(f"              → matched card: {line['_card_desc']}")
        print(f"              → x{line['quantity']} @ ${line['sale_price']:.2f} ({condition}) — paid {line['paid_at']}")
        return "recorded"

    try:
        cur.execute("SAVEPOINT sp_process_line")
        sale_notes = f"{line['title']} | var: {line['variation_name']}"
        cur.execute(
            """
            SELECT record_sale(
                p_variant_id        := %s,
                p_condition         := %s,
                p_quantity          := %s,
                p_sale_price        := %s,
                p_platform          := 'ebay',
                p_account           := %s,
                p_sold_at           := %s,
                p_platform_order_id := %s,
                p_notes             := %s,
                p_listing_id        := %s,
                p_external_id       := %s,
                p_order_line_item_id := %s
            ) AS result
            """,
            (
                variant_id, condition, line["quantity"], line["sale_price"],
                account, line["paid_at"], line["order_id"],
                sale_notes, line["item_id"], line["variation_name"],
                line["order_line_item_id"],
            ),
        )
        result = cur.fetchone()["result"]
        if isinstance(result, str):
            result = json.loads(result)
        cur.execute("RELEASE SAVEPOINT sp_process_line")
    except Exception as e:
        # Roll back to the savepoint (not the whole transaction) — this is
        # what actually failed before: an error here used to poison the
        # ENTIRE run's transaction, which meant _file_issue's own INSERT
        # would itself fail with InFailedSqlTransaction, which meant the
        # exception propagated all the way up uncaught, which meant
        # _set_last_pull() never ran, which meant the watermark never
        # advanced, which meant the SAME poisoned line got refetched and
        # recrashed every 15 minutes forever. The savepoint breaks that
        # chain at its root: whatever failed above is undone, but everything
        # else this run already recorded stays committed, and _file_issue
        # below runs against a healthy transaction.
        cur.execute("ROLLBACK TO SAVEPOINT sp_process_line")
        msg = str(e)
        reason = "insufficient_stock" if "Insufficient stock" in msg else "error"
        _file_issue(cur, line, account, reason, msg, dry_run)
        return reason

    # Sale recorded. If the listing decrement found no platform_listings row,
    # the sale still stands — but queue the bookkeeping gap for manual review.
    # (listing_gap is NOT auto-retried: the sale exists, so a retry would just
    # hit dedup and false-resolve the issue.)
    if result.get("listing_updated") is False:
        _file_issue(
            cur, line, account, "listing_gap",
            f"sale recorded, but no platform_listings row for item {line['item_id']} "
            f"/ {line['variation_name']!r}",
            dry_run,
        )
        return "recorded_with_gap"

    # If this line was a previously-open issue, mark it resolved
    cur.execute(
        """
        UPDATE ebay_order_issues
        SET status = 'resolved', updated_at = now()
        WHERE order_line_item_id = %s AND status = 'open'
        """,
        (line["order_line_item_id"],),
    )
    return "recorded"


def _file_issue(cur, line: dict, account: str, reason: str, detail: str, dry_run: bool):
    if dry_run:
        print(f"    [dry-run] would file issue ({reason}): {line['title'][:40]} — {detail}")
        return
    cur.execute(
        """
        INSERT INTO ebay_order_issues (
            order_line_item_id, order_id, item_id, variation_name, title,
            account, quantity, sale_price, paid_at, reason, detail
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (order_line_item_id) DO UPDATE
            SET reason = EXCLUDED.reason,
                detail = EXCLUDED.detail,
                status = 'open',
                updated_at = now()
        """,
        (
            line["order_line_item_id"], line["order_id"], line["item_id"],
            line["variation_name"], line["title"], account,
            line["quantity"], line["sale_price"], line["paid_at"],
            reason, detail,
        ),
    )


# ══════════════════════════════════════════════════════════════════════════════
# Step 0 — Retry open issues from previous runs
# ══════════════════════════════════════════════════════════════════════════════

def retry_open_issues(cur, account: str, dry_run: bool, paid_floor: datetime = None) -> dict:
    cur.execute(
        """
        SELECT order_line_item_id, order_id, item_id, variation_name, title,
               quantity, sale_price, paid_at
        FROM ebay_order_issues
        WHERE status = 'open' AND account = %s
          AND reason NOT IN ('listing_gap', 'pre_inventory',
                             'cancelled', 'cancelled_after_recording')  -- not auto-retried; manual review only
        ORDER BY created_at
        """,
        (account,),
    )
    rows = cur.fetchall()
    counts = {"recorded": 0, "still_open": 0}

    for r in rows:
        line = {
            "order_line_item_id": r["order_line_item_id"], "order_id": r["order_id"],
            "item_id": r["item_id"], "variation_name": r["variation_name"],
            "title": r["title"], "quantity": r["quantity"],
            "sale_price": float(r["sale_price"]),
            "paid_at": r["paid_at"].isoformat() if hasattr(r["paid_at"], "isoformat") else r["paid_at"],
        }
        result = _process_line(cur, line, account, dry_run, paid_floor)
        if result in ("recorded", "recorded_with_gap", "duplicate"):
            counts["recorded"] += 1
            if result == "duplicate" and not dry_run:
                # already in sales somehow — close the issue
                cur.execute(
                    "UPDATE ebay_order_issues SET status = 'resolved', updated_at = now() WHERE order_line_item_id = %s",
                    (line["order_line_item_id"],),
                )
        else:
            counts["still_open"] += 1

    return counts


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

def pull_orders(account_num: int = 1, since_str: str = None, until_str: str = None,
                order_ids: list[str] = None, dry_run: bool = False,
                paid_since_str: str = None, quiet: bool = False) -> bool:
    """
    Returns True if anything needing manual attention was filed this run
    (unmatched, insufficient_stock, or error) — main.py uses this to set a
    nonzero exit code for scheduled runs (Task Scheduler / cron), so a
    failure can eventually be hooked up to a notification without
    parsing log text.
    """
    account = get_account_name(account_num)
    run_start = datetime.now(timezone.utc)

    def p(msg):
        if not quiet:
            print(msg)

    p(f"\n{'═'*60}")
    p(f"📦 eBay order pull — account {account_num} ({account})"
      + (" [DRY RUN]" if dry_run else ""))
    p(f"{'═'*60}")

    with db_cursor() as cur:
        # Paid-date floor — persists across runs once set. An explicit
        # --paid-since updates the stored value; otherwise fall back to
        # whatever was set previously (if anything).
        if paid_since_str:
            paid_floor = datetime.fromisoformat(paid_since_str).replace(tzinfo=timezone.utc)
            if not dry_run:
                _set_paid_floor(cur, account_num, paid_floor)
            p(f"🛡  Paid-floor: {paid_floor.isoformat()} (set via --paid-since)")
        else:
            paid_floor = _get_paid_floor(cur, account_num)
            if paid_floor:
                p(f"🛡  Paid-floor: {paid_floor.isoformat()} (persisted from earlier run)")
            else:
                p("🛡  Paid-floor: none set — every paid order is eligible for recording")

        since = until = None
        targeted = bool(order_ids)

        if targeted:
            p(f"🎯 Targeted pull: {len(order_ids)} order ID(s) — no time window")
        else:
            # Resolve the window: CLI --since > sync_state > start of today (UTC)
            if since_str:
                since = datetime.fromisoformat(since_str).replace(tzinfo=timezone.utc)
                p(f"⏱  Window: --since {since.isoformat()} (manual override)")
            else:
                last = _get_last_pull(cur, account_num)
                if last:
                    since = last
                    p(f"⏱  Window: since last pull {since.isoformat()}")
                else:
                    since = run_start.replace(hour=0, minute=0, second=0, microsecond=0)
                    p(f"⏱  Window: first run — since start of today {since.isoformat()}")
            if until_str:
                until = datetime.fromisoformat(until_str).replace(tzinfo=timezone.utc)
                p(f"⏱  Until: {until.isoformat()}")

        # Retry anything left open from previous runs
        retried = retry_open_issues(cur, account, dry_run, paid_floor)
        if retried["recorded"] or retried["still_open"]:
            p(f"🔁 Retried open issues: {retried['recorded']} recorded, {retried['still_open']} still open")

        # Pull + process
        p("🔍 Fetching orders from eBay...")
        lines = fetch_orders(since=since, until=until, order_ids=order_ids,
                             account_num=account_num)
        p(f"   → {len(lines)} order line(s)\n")

        counts = {"recorded": 0, "recorded_with_gap": 0, "duplicate": 0,
                  "unmatched": 0, "insufficient_stock": 0, "pre_inventory": 0,
                  "cancelled": 0, "cancelled_after_recording": 0, "error": 0}

        for line in lines:
            result = _process_line(cur, line, account, dry_run, paid_floor)
            counts[result] = counts.get(result, 0) + 1
            if result == "recorded" and not dry_run:
                card_desc = line.get("_card_desc", "")
                p(f"  ✅ order {line['order_id']} — {card_desc} "
                  f"x{line['quantity']} @ ${line['sale_price']:.2f} — paid {line['paid_at']}")
            elif result == "recorded_with_gap":
                card_desc = line.get("_card_desc", "")
                p(f"  ✅⚠️ order {line['order_id']} — {card_desc} "
                  f"x{line['quantity']} @ ${line['sale_price']:.2f} — paid {line['paid_at']} "
                  f"— sale recorded, no listing row (queued)")
            elif result == "unmatched":
                p(f"  ⚠️  unmatched: {line['title'][:44]} (item {line['item_id']})")
            elif result == "insufficient_stock":
                p(f"  ❌ insufficient stock: {line['title'][:44]} x{line['quantity']}")
            elif result == "pre_inventory":
                p(f"  🛡  pre-inventory (skipped): {line['title'][:40]} — paid {line['paid_at']}")
            elif result == "cancelled":
                p(f"  🚫 cancelled (never recorded, filed for visibility): {line['title'][:40]}")
            elif result == "cancelled_after_recording":
                p(f"  🚫⚠️ CANCELLED AFTER RECORDING — inventory may need manual reversal: "
                  f"{line['title'][:44]} x{line['quantity']} (order {line['order_id']})")

        # Advance the watermark to run start (not 'now') so nothing that
        # arrived mid-run falls into a gap next time. Targeted (--order) and
        # bounded (--until) pulls don't move it — they're manual backfills,
        # not the ongoing sync.
        if not dry_run and not targeted and not until:
            _set_last_pull(cur, account_num, run_start)

    needs_attention = bool(counts["unmatched"] or counts["insufficient_stock"]
                           or counts["error"] or counts["cancelled_after_recording"])
    total_recorded = counts['recorded'] + counts['recorded_with_gap']

    # In quiet mode: always print exactly one summary line. Print the full
    # breakdown too, but only when something actually needs a look — a
    # scheduled run with nothing but recorded/duplicate stays a single line
    # in the log.
    if quiet:
        print(f"[{run_start.isoformat()}] pull account {account_num}"
              + (" [DRY RUN]" if dry_run else "") + ": "
              f"recorded={total_recorded} duplicate={counts['duplicate']} "
              f"unmatched={counts['unmatched']} insufficient_stock={counts['insufficient_stock']} "
              f"pre_inventory={counts['pre_inventory']} cancelled={counts['cancelled']} "
              f"cancelled_after_recording={counts['cancelled_after_recording']} error={counts['error']}")
        if needs_attention:
            print(f"   → Filed in ebay_order_issues (status = 'open'); fix the cause and re-run to retry.")
    else:
        print(f"\n{'─'*60}")
        print(f"📊 Recorded: {total_recorded} ({counts['recorded_with_gap']} with listing gap) | Duplicates skipped: {counts['duplicate']}")
        print(f"   Unmatched: {counts['unmatched']} | Insufficient stock: {counts['insufficient_stock']} | "
              f"Pre-inventory (skipped): {counts['pre_inventory']} | Errors: {counts['error']}")
        print(f"   Cancelled (never recorded): {counts['cancelled']} | "
              f"Cancelled after recording: {counts['cancelled_after_recording']}")
        if needs_attention:
            print(f"   → Filed in ebay_order_issues (status = 'open'); fix the cause and re-run to retry.")
        if counts["recorded_with_gap"]:
            print(f"   → Listing gaps filed in ebay_order_issues; sales are recorded, fix the listing rows manually.")
        if counts["pre_inventory"]:
            print(f"   → Pre-inventory lines filed in ebay_order_issues (reason='pre_inventory'); "
                  f"these predate your paid-floor cutoff and were intentionally not recorded.")
        if counts["cancelled_after_recording"]:
            print(f"   → ⚠️  {counts['cancelled_after_recording']} order(s) were cancelled AFTER their sale was "
                  f"recorded — inventory may be overstated. Review in Issues (reason='cancelled_after_recording') "
                  f"and reverse manually if appropriate.")
        if dry_run:
            print("   (dry run — nothing was written)")

    return needs_attention


# ══════════════════════════════════════════════════════════════════════════════
# Backfill — one-time historical fix for the OrderLineItemID / OrderID split
# ══════════════════════════════════════════════════════════════════════════════

def backfill_order_ids(account_num: int = 1, since_str: str = None,
                       until_str: str = None, dry_run: bool = False) -> None:
    """
    Rows recorded before the OrderLineItemID / OrderID split have the
    line-item ID in platform_order_id. Re-fetch orders from eBay and,
    matching on order_line_item_id, set platform_order_id to the real
    order-level OrderID.

    Safe to re-run: only touches rows where platform_order_id still equals
    the line-item id, so already-fixed and newly-recorded rows are no-ops.
    """
    account = get_account_name(account_num)
    print(f"\n🔧 Backfill order IDs — account {account_num} ({account})"
          + (" [DRY RUN]" if dry_run else ""))

    since = None
    until = None
    if since_str:
        since = datetime.fromisoformat(since_str).replace(tzinfo=timezone.utc)
    if until_str:
        until = datetime.fromisoformat(until_str).replace(tzinfo=timezone.utc)

    lines = fetch_orders(since=since, until=until, account_num=account_num)
    print(f"   fetched {len(lines)} order line(s) from eBay")

    updated = skipped = 0
    with db_cursor() as cur:
        for line in lines:
            if not line.get("order_id") or not line.get("order_line_item_id"):
                continue
            if line["order_id"] == line["order_line_item_id"]:
                skipped += 1   # legacy single-line orders where they coincide
                continue
            if dry_run:
                cur.execute(
                    """
                    SELECT count(*) AS n FROM sales
                    WHERE platform = 'ebay'
                      AND order_line_item_id = %s
                      AND platform_order_id = order_line_item_id
                    """,
                    (line["order_line_item_id"],),
                )
                n = cur.fetchone()["n"]
                if n:
                    print(f"   [dry-run] would update {n} row(s): "
                          f"{line['order_line_item_id']} -> order {line['order_id']}")
                    updated += n
            else:
                cur.execute(
                    """
                    UPDATE sales
                    SET platform_order_id = %s
                    WHERE platform = 'ebay'
                      AND order_line_item_id = %s
                      AND platform_order_id = order_line_item_id
                    """,
                    (line["order_id"], line["order_line_item_id"]),
                )
                updated += cur.rowcount

    print(f"   done: {updated} row(s) {'would be ' if dry_run else ''}updated, "
          f"{skipped} skipped (order id == line item id)")
