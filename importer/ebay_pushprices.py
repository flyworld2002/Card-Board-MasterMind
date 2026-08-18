"""
importer/ebay_pushprices.py — pushes resolved prices/quantities for one
eBay listing via the Listing Pricing System
(docs/plans/listing-pricing-system.md).

As of the roster/groups pivot, this is the ONE push command for the
feature (absorbed the 250-cap promotion logic that used to live in
importer/ebay_listing_sync.py's now-removed --ebay-push-listings, since
both walked the same listing_card_assignments roster).

Resolution (price, quantity, source) always comes from the
resolve_listing_prices(platform, listing_id) Postgres RPC — never
recomputed here — so the web pricing grid and this CLI job can never
disagree. The roster (listing_card_assignments) is the source of truth
for which cards belong to a listing, including 'queued' cards with no
live platform_listings row yet.

Gating: only rows that are 'active' (have a live platform_listings row)
AND sync_enabled=true AND status='active' AND not blocked by the
platform_sync_status kill switch are ever pushed — mirrors
ebay_listing_sync.py's _resolve_scope. Quantity gating: available_qty is
reduced by low_stock_qty (floored at 0) before being sent.
"""

import uuid

from db.connection import db_cursor
from importer.ebay_auth import get_account_name, get_user_token
from importer.ebay import _post, _find, _findall
from importer.ebay_variations_xml import (
    fetch_item, deep_copy_variations, strip_selling_status, normalize_quantities,
    find_variation_by_specifics, set_variation_price_qty, get_specifics_set,
    insert_specifics_value, mark_variation_deleted, add_variation_row,
    set_variation_picture, build_revise_xml,
)
from importer.ebay_listing_sync import (
    platform_sync_allowed, _render_variation_name, _compute_insert_position,
)
from importer.ebay_pictures import upload_picture_from_url, upload_picture_bytes
from importer.card_photos import resolve_photo_urls


def _resolve_template(cur, platform: str, listing_id: str):
    cur.execute(
        "SELECT id, listing_kind, display_sort, default_quantity_limit "
        "FROM listing_templates WHERE platform = %s AND listing_id = %s",
        (platform, listing_id),
    )
    return cur.fetchone()


def _compute_roster_changes(cur, platform: str, listing_id: str):
    """
    Returns (resolved_rows, changes, skipped_ungated) — same contract as
    before the roster pivot, except row_id is now
    listing_card_assignments.id, and only rows with a live
    platform_listings row (status 'active' or 'out_of_stock') are ever
    eligible for `changes` — queued rows show up in `resolved` for
    preview but are never pushed directly (only relevant via 250-cap
    promotion). An 'out_of_stock' row is always pushed (never skipped)
    with whatever qty_to_push actually computes to — eBay's stored
    quantity should always reflect current truth, whether that's really
    zero or the row has since been restocked (8/12: status is a cached
    label derived from quantity, not an independent gate — see below).
    """
    cur.execute("SELECT * FROM resolve_listing_prices(%s, %s)", (platform, listing_id))
    resolved = cur.fetchall()
    if not resolved:
        return [], [], []

    active_ids = [str(r["platform_listing_id"]) for r in resolved if r["platform_listing_id"]]
    current_by_id = {}
    if active_ids:
        cur.execute(
            "SELECT id, external_id, pushed_price, pushed_qty, sync_enabled, status, account "
            "FROM platform_listings WHERE id = ANY(%s::uuid[])",
            (active_ids,),
        )
        current_by_id = {r["id"]: r for r in cur.fetchall()}

    accounts_present = {r["account"] for r in current_by_id.values()} | {None}
    kill_switch_ok = {a: platform_sync_allowed(cur, platform, a) for a in accounts_present}

    changes = []
    skipped_ungated = []
    for r in resolved:
        if r["status"] != "active" or not r["platform_listing_id"]:
            continue  # queued/sold_out_retained rows aren't push candidates
        cur_row = current_by_id.get(r["platform_listing_id"])
        if cur_row is None:
            continue

        available = r["available_qty"] or 0
        qty_to_push = available
        if r["low_stock_qty"] is not None:
            qty_to_push = max(available - r["low_stock_qty"], 0)
        if r["quantity_limit"] is not None:
            qty_to_push = min(qty_to_push, r["quantity_limit"])

        # No status-based override here on purpose (8/12 fix): qty_to_push
        # is already the current truth computed from available_qty above.
        # A genuinely still-empty out_of_stock row naturally computes to 0
        # here (nothing forced), which correctly zeroes eBay's stored
        # quantity the same as before. A row that's been restocked since
        # going OOS (e.g. new inventory imported) now correctly pushes its
        # real quantity instead of being silently held at a stale forced
        # 0 forever — platform_listings.status is a cached label derived
        # FROM quantity by every writer already; forcing it here to gate
        # quantity was backwards. The write-back below self-heals status
        # to match whatever actually got pushed, same pattern
        # revise_single_variation_qty() (Balance Qty) already uses.

        # Always recompute and resend every gated-in row's price/qty fresh
        # from what's currently resolved, rather than only sending rows
        # that differ from our own last-recorded pushed_price/pushed_qty.
        # That diff-against-our-own-history approach is exactly what let
        # eBay-side drift go undetected tonight (2026-08) — our own
        # records said a row was already correct, so it silently never
        # got resent even after eBay's live quantity drifted away from
        # what we last told it. Always pushing the current truth is
        # self-healing against that kind of drift, whatever the cause.
        change = {
            "row_id": r["row_id"],
            "platform_listing_id": r["platform_listing_id"],
            "external_id": cur_row["external_id"],
            "derived_label": r["derived_label"],
            "resolved_price": float(r["resolved_price"]),
            "price_source": r["price_source"],
            "qty_to_push": qty_to_push,
        }

        # out_of_stock is gated in same as active — it's only 'delisted'
        # (no longer live on eBay at all, nothing to revise) that's
        # excluded on status now.
        gated_in = (
            cur_row["sync_enabled"]
            and cur_row["status"] in ("active", "out_of_stock")
            and kill_switch_ok.get(cur_row["account"], True)
            and kill_switch_ok.get(None, True)
        )
        if gated_in:
            changes.append(change)
        else:
            reason = (
                "platform/account sync disabled"
                if not (kill_switch_ok.get(cur_row["account"], True) and kill_switch_ok.get(None, True))
                else "sync_enabled=false" if not cur_row["sync_enabled"]
                else f"status={cur_row['status']!r}"
            )
            skipped_ungated.append({**change, "reason": reason})

    return resolved, changes, skipped_ungated


def _stage_promotion(cur, template, platform: str, listing_id: str, promote, promoted_resolved,
                      variations, specific_name: str, display_sort: str, account: str):
    """
    Mutates `variations` in-memory to add one new <Variation> row for
    `promote` (a queued listing_card_assignments row — anything with
    ["id"] and ["variant_id"]). Purely in-memory, always safe to call —
    has no live effect until the caller actually POSTs the Revise call.

    Returns (promotion, pending_writes). `pending_writes` is a list of
    (sql, params) tuples the caller MUST NOT execute until AFTER a
    successful live eBay Revise call — running them before that (e.g. for
    --dry-run, or if the POST fails) would leave the DB believing a card
    is live on eBay when it never actually was. The new platform_listings
    row's id is generated client-side (uuid.uuid4()) specifically so the
    INSERT + the two dependent UPDATEs can all be pre-built as plain
    parameterized tuples and deferred as a unit, instead of needing a
    RETURNING round-trip before the rest of the writes can be built.

    If `promote` carries a non-null "custom_name" (listing_card_assignments.
    custom_name — a per-card override, same pin pattern as manual_price
    etc.), it's used verbatim instead of calling _render_variation_name()
    at all — lets a card get an exact hand-typed eBay variation name
    (e.g. matching a listing's alpha-sort word order, or promo wording
    _render_variation_name() has no token for) instead of whatever the
    format-string default would produce.

    If `promote` carries a non-null "eps_picture_url" (staged via
    stage_card_picture() when the user clicked the thumbnail before this
    card ever went live), it's passed back in the returned promotion dict
    but deliberately NOT applied to `variations` here — the caller must
    call set_variation_picture() for every promotion in the batch only
    AFTER every add_variation_row() call has finished (see that
    function's docstring for why: <Pictures> must land after every
    <Variation>, and this function may be called several times in a loop
    for one batch).

    Also stages an ebay_listing_map upsert (item_id, variation_name) ->
    variant_id — that table is what importer/ebay_orders.py's sale
    matcher reads on every order pull, and NOTHING else in this codebase
    writes to it, so without this a card pushed live here would silently
    fail to match its own future sale ("unmatched" issue) even though it
    really is live. Condition is always 'Near Mint', matching every other
    real row in this project (market_prices, inventory) — see
    docs/plans/listing-pricing-system.md.
    """
    promoted_name = promote.get("custom_name") or _render_variation_name(cur, promote["variant_id"], template["id"])
    position = _compute_insert_position(cur, variations, specific_name, listing_id,
                                         promote["variant_id"], display_sort)
    insert_specifics_value(variations, specific_name, promoted_name, position=position)

    resolved_price = float(promoted_resolved["resolved_price"]) if promoted_resolved else 0.0
    available = (promoted_resolved["available_qty"] or 0) if promoted_resolved else 0
    low_stock = promoted_resolved["low_stock_qty"] if promoted_resolved else None
    qty_limit = promoted_resolved["quantity_limit"] if promoted_resolved else None
    qty_to_push = max(available - low_stock, 0) if low_stock is not None else available
    if qty_limit is not None:
        qty_to_push = min(qty_to_push, qty_limit)

    add_variation_row(variations, {specific_name: promoted_name}, quantity=qty_to_push,
                       start_price=resolved_price)

    new_platform_listing_id = str(uuid.uuid4())
    pending_writes = [
        (
            """
            INSERT INTO platform_listings
                (id, platform, account, listing_id, external_id, variant_id, list_price,
                 quantity_listed, status, sync_enabled, template_id, listed_at,
                 pushed_price, pushed_qty, pushed_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'active', true, %s, now(), %s, %s, now())
            """,
            (new_platform_listing_id, platform, account, listing_id, promoted_name,
             promote["variant_id"], resolved_price, qty_to_push, template["id"],
             resolved_price, qty_to_push),
        ),
        (
            "UPDATE listing_card_assignments SET status = 'active', platform_listing_id = %s, updated_at = now() "
            "WHERE id = %s",
            (new_platform_listing_id, promote["id"]),
        ),
        (
            """
            INSERT INTO ebay_listing_map
                (item_id, listing_id, variation_name, variant_id, condition, source, last_synced_at)
            VALUES (%s, %s, %s, %s, 'Near Mint', 'push', now())
            ON CONFLICT (item_id, variation_name) DO UPDATE
                SET variant_id = EXCLUDED.variant_id,
                    condition = EXCLUDED.condition,
                    source = EXCLUDED.source,
                    last_synced_at = now()
            """,
            (listing_id, listing_id, promoted_name, promote["variant_id"]),
        ),
    ]

    promotion = {
        "row_id": promote["id"],
        "platform_listing_id": new_platform_listing_id,
        "external_id": promoted_name,
        "derived_label": promoted_resolved["derived_label"] if promoted_resolved else promoted_name,
        "resolved_price": resolved_price,
        "price_source": promoted_resolved["price_source"] if promoted_resolved else "default",
        "qty_to_push": qty_to_push,
        "position": position,
        "eps_picture_url": promote.get("eps_picture_url"),
        "card_photo_id": promote.get("card_photo_id"),
    }
    return promotion, pending_writes


def _do_promotions(cur, template, platform: str, listing_id: str, account_num: int,
                    resolved: list, variations, quiet: bool):
    """
    Adds new <Variation> rows for queued cards, two cases:
      1. Room under eBay's 250-variation cap (active count < 250):
         promote directly, no deletion needed — can promote several
         queued rows in the same call.
      2. At cap (active count >= 250): free a slot by deleting a
         sold-out active variation first, one-for-one swap (the original
         250-cap holdback design).
    Mutates `variations` in place (always safe — in-memory only). Returns
    (promotions, pending_writes) — see _stage_promotion's docstring for
    why pending_writes must only be executed after a successful live
    Revise call, never for --dry-run.
    """
    def p(msg):
        if not quiet:
            print(msg)

    cur.execute(
        "SELECT id, variant_id, priority_rank, custom_name, eps_picture_url, card_photo_id "
        "FROM listing_card_assignments "
        "WHERE template_id = %s AND status = 'queued' ORDER BY priority_rank ASC",
        (template["id"],),
    )
    queued = list(cur.fetchall())
    if not queued:
        return [], []

    resolved_by_row_id = {r["row_id"]: r for r in resolved}
    specifics_set = get_specifics_set(variations)
    specific_name = next(iter(specifics_set), None)
    if specific_name is None:
        return [], []
    display_sort = template.get("display_sort") or "card_number"

    promotions = []
    pending_writes = []

    # Case 1: direct promotion — room under the cap, no deletion needed.
    active_count = sum(1 for r in resolved if r["status"] == "active")
    room = max(250 - active_count, 0)
    if room > 0 and queued:
        cur.execute(
            "SELECT DISTINCT account FROM platform_listings "
            "WHERE platform = %s AND listing_id = %s AND account IS NOT NULL LIMIT 1",
            (platform, listing_id),
        )
        acct_row = cur.fetchone()
        account = acct_row["account"] if acct_row else None

        while queued and room > 0:
            promote = queued.pop(0)
            promoted_resolved = resolved_by_row_id.get(promote["id"])
            if promoted_resolved is None:
                continue
            if not promoted_resolved["available_qty"]:
                # eBay silently drops a variation added at quantity 0
                # (Ack=Warning, "Variations with quantity '0' will be
                # removed" — see push_single_card_live()) — leave it
                # queued rather than record a phantom promotion.
                p(f"    [SKIP] {promoted_resolved['derived_label']}: 0 available quantity, "
                  f"leaving queued")
                continue
            promotion, writes = _stage_promotion(
                cur, template, platform, listing_id, promote, promoted_resolved,
                variations, specific_name, display_sort, account,
            )
            promotions.append(promotion)
            pending_writes.extend(writes)
            room -= 1
            p(f"    [PROMOTE] (free slot) -> {promotion['external_id']!r} "
              f"at position {promotion['position'] if promotion['position'] is not None else 'end'}")

    # Case 2: at cap — free a slot by deleting a sold-out active variation.
    if queued:
        active_rows_sql = [r for r in resolved if r["status"] == "active" and r["platform_listing_id"]]
        cur.execute(
            "SELECT id, external_id, account FROM platform_listings WHERE id = ANY(%s::uuid[])",
            ([str(r["platform_listing_id"]) for r in active_rows_sql],),
        )
        listing_meta = {r["id"]: r for r in cur.fetchall()}

        for r in active_rows_sql:
            if not queued:
                break
            if (r["available_qty"] or 0) > 0:
                continue  # not sold out, never delete a row with stock

            meta = listing_meta.get(r["platform_listing_id"])
            if not meta:
                continue
            var_el = find_variation_by_specifics(variations, specific_name, meta["external_id"])
            if var_el is None:
                continue

            promote = queued.pop(0)
            promoted_resolved = resolved_by_row_id.get(promote["id"])
            if promoted_resolved is None:
                continue
            if not promoted_resolved["available_qty"]:
                # Same as Case 1 — don't burn a sold-out slot's deletion
                # on a card eBay would just reject at quantity 0.
                p(f"    [SKIP] {promoted_resolved['derived_label']}: 0 available quantity, "
                  f"leaving queued")
                continue

            mark_variation_deleted(var_el)
            promotion, writes = _stage_promotion(
                cur, template, platform, listing_id, promote, promoted_resolved,
                variations, specific_name, display_sort, meta["account"],
            )
            promotions.append(promotion)
            pending_writes.append((
                "UPDATE listing_card_assignments SET status = 'sold_out_retained', updated_at = now() WHERE id = %s",
                (r["row_id"],),
            ))
            pending_writes.extend(writes)
            p(f"    [PROMOTE] {meta['external_id']} (sold out) -> {promotion['external_id']!r} "
              f"at position {promotion['position'] if promotion['position'] is not None else 'end'}")

    # Pictures are applied in a single pass here, after every
    # add_variation_row()/mark_variation_deleted() call above has already
    # happened for this whole batch — set_variation_picture() requires
    # <Pictures> to land after every <Variation> element, and this
    # function may have staged several promotions above.
    for promotion in promotions:
        photo_urls = resolve_photo_urls(cur, promotion)
        if photo_urls:
            set_variation_picture(variations, promotion["external_id"], photo_urls)

    return promotions, pending_writes


def push_prices(listing_id: str, account_num: int = 1, platform: str = "ebay",
                 dry_run: bool = False, quiet: bool = False) -> dict:
    """
    Returns a summary dict (used by the /push-prices API endpoint, same
    convention as importer.ebay_picking.pull_picking):
      {"listing_id", "resolved", "changed", "pushed", "warnings": [...],
       "dry_run": bool}
    """
    def p(msg):
        if not quiet:
            print(msg)

    with db_cursor() as cur:
        template = _resolve_template(cur, platform, listing_id)
        if template is None:
            print(f"No listing_templates row for {platform} listing {listing_id} — "
                  f"create one (set its listing_id) before pushing.")
            return {"listing_id": listing_id, "resolved": 0, "changed": 0, "pushed": 0, "promoted": 0,
                    "warnings": ["no template for this listing_id"], "dry_run": dry_run}

        resolved, changes, skipped_ungated = _compute_roster_changes(cur, platform, listing_id)
        summary = {"listing_id": listing_id, "resolved": len(resolved), "changed": len(changes),
                   "pushed": 0, "promoted": 0, "warnings": [], "dry_run": dry_run}

        if not resolved:
            p(f"No roster (listing_card_assignments) rows found for {platform} listing {listing_id}.")
            return summary

        if skipped_ungated:
            for s in skipped_ungated:
                msg = f"{s['derived_label']} ({s['external_id']}): would change but not synced — {s['reason']}"
                summary["warnings"].append(msg)
                p(f"  [NOT SYNCED] {msg}")

        needs_promotion_check = len(resolved) > 250 or any(r["status"] == "queued" for r in resolved)
        if not changes and not needs_promotion_check:
            p(f"[{listing_id}] nothing gated-in with pending changes "
              f"({len(resolved)} row(s) resolved, {len(skipped_ungated)} skipped as not synced) — nothing to push.")
            return summary

        if template["listing_kind"] == "single":
            if not changes:
                p(f"[{listing_id}] single listing, no gated changes — nothing to push.")
                return summary
            change = changes[0]

            p(f"  [single] {change['derived_label']}: ${change['resolved_price']:.2f}, "
              f"qty -> {change['qty_to_push']} [{change['price_source']}]")

            if dry_run:
                p(f"[DRY-RUN] would push 1 change to {listing_id}")
                return summary

            token = get_user_token(account_num=account_num)
            xml = f"""<?xml version="1.0" encoding="utf-8"?>
<ReviseFixedPriceItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials>
    <eBayAuthToken>{token}</eBayAuthToken>
  </RequesterCredentials>
  <Item>
    <ItemID>{listing_id}</ItemID>
    <StartPrice>{change['resolved_price']:.2f}</StartPrice>
    <Quantity>{change['qty_to_push']}</Quantity>
  </Item>
</ReviseFixedPriceItemRequest>"""
            _post("ReviseFixedPriceItem", xml, account_num=account_num)
            # quantity_listed must track every push, not just pushed_qty — it's
            # the only column resolve_listing_prices()'s shared-inventory
            # subtraction reads to know how much of a variant another listing
            # is currently holding. Missing here for a long time (found
            # 2026-08-17): 9,197 active rows had drifted stale, silently
            # making every OTHER listing that shares a card think more of it
            # was available than actually was.
            cur.execute(
                "UPDATE platform_listings SET pushed_price = %s, pushed_qty = %s, quantity_listed = %s, "
                "pushed_at = now(), "
                "status = CASE WHEN status = 'delisted' THEN status "
                "WHEN %s > 0 THEN 'active' ELSE 'out_of_stock' END WHERE id = %s",
                (change["resolved_price"], change["qty_to_push"], change["qty_to_push"], change["qty_to_push"],
                 change["platform_listing_id"]),
            )
            summary["pushed"] = 1
            p(f"[{listing_id}] pushed 1 change.")
            return summary

        # Multi-variation path
        item = fetch_item(listing_id, account_num=account_num)
        variations_node = _find(item, "Variations")
        if variations_node is None:
            p(f"[{listing_id}] template says listing_kind='variation' but the live listing has no "
              f"<Variations> block — mismatch, needs manual reconcile.")
            summary["warnings"].append("listing_kind='variation' but no live <Variations> block")
            return summary

        variations = deep_copy_variations(item)
        normalize_quantities(variations)
        strip_selling_status(variations)

        specifics_set = get_specifics_set(variations)
        specific_name = next(iter(specifics_set), None)

        changes_pushed = []
        for change in changes:
            if specific_name is None or not change["external_id"]:
                msg = f"{change['derived_label']}: no specifics name / external_id, skipping"
                summary["warnings"].append(msg)
                p(f"  [WARN] {msg}")
                continue
            var_el = find_variation_by_specifics(variations, specific_name, change["external_id"])
            if var_el is None:
                msg = f"variation {change['external_id']!r} not found live — mismatch, needs manual reconcile in Seller Hub"
                summary["warnings"].append(msg)
                p(f"  [WARN] {msg}")
                continue

            set_variation_price_qty(var_el, start_price=change["resolved_price"], quantity=change["qty_to_push"])
            changes_pushed.append(change)
            p(f"  {change['external_id']}: ${change['resolved_price']:.2f}, qty -> {change['qty_to_push']} "
              f"[{change['price_source']}]")

        # promotions' DB writes (pending_writes) are deferred — mutating
        # `variations` here is always safe (in-memory only), but nothing
        # about a promotion may be written to the DB until AFTER a real,
        # successful eBay Revise call below. Doing it earlier (the
        # previous implementation executed these immediately) meant
        # --dry-run — or a POST that later failed — would still leave the
        # DB believing a card had gone live when eBay never received it.
        promotions, pending_writes = _do_promotions(cur, template, platform, listing_id, account_num,
                                                      resolved, variations, quiet)
        for promo in promotions:
            p(f"  {promo['external_id']}: ${promo['resolved_price']:.2f}, qty -> {promo['qty_to_push']} "
              f"(newly promoted) [{promo['price_source']}]")

        pushed = changes_pushed + promotions
        if not pushed:
            p(f"[{listing_id}] {len(changes)} change(s) computed, but none matched a live variation "
              f"and no promotions triggered — nothing pushed.")
            return summary

        if dry_run:
            p(f"[DRY-RUN] would push {len(pushed)} of {len(resolved)} row(s) to {listing_id} "
              f"({len(promotions)} via 250-cap promotion)")
            summary["pushed"] = len(pushed)
            summary["promoted"] = len(promotions)
            return summary

        xml = build_revise_xml(listing_id, variations, "ReviseFixedPriceItem", account_num=account_num)
        _post("ReviseFixedPriceItem", xml, account_num=account_num)

        for change in changes_pushed:
            cur.execute(
                "UPDATE platform_listings SET pushed_price = %s, pushed_qty = %s, quantity_listed = %s, "
                "pushed_at = now(), "
                "status = CASE WHEN status = 'delisted' THEN status "
                "WHEN %s > 0 THEN 'active' ELSE 'out_of_stock' END WHERE id = %s",
                (change["resolved_price"], change["qty_to_push"], change["qty_to_push"], change["qty_to_push"],
                 change["platform_listing_id"]),
            )
        for sql, params in pending_writes:
            cur.execute(sql, params)

        summary["pushed"] = len(pushed)
        summary["promoted"] = len(promotions)
        p(f"[{listing_id}] pushed {len(pushed)} of {len(resolved)} row(s) ({len(promotions)} promoted).")
        return summary


def push_single_card_live(row_id: str, account_num: int = 1, platform: str = "ebay",
                           dry_run: bool = False, quiet: bool = False) -> dict:
    """
    Pushes ONE queued roster row live as a brand-new <Variation> on its
    listing — reuses _stage_promotion, the same helper push_prices()'s
    250-cap promotion uses, so the two paths can never diverge in how a
    promoted row is priced/quantified or written to the DB. Deliberately
    separate from push_prices()'s general diff-and-push flow: this is an
    explicit, single-card action a user chooses to take right now, not a
    scheduled sync, and it must NOT touch any other variation's price or
    quantity on the live listing — only the deep-copied <Variations> tree
    is mutated, and only to append one new row.
    """
    def p(msg):
        if not quiet:
            print(msg)

    with db_cursor() as cur:
        cur.execute(
            "SELECT lca.id, lca.variant_id, lca.status, lca.custom_name, lca.eps_picture_url, lca.card_photo_id, "
            "lt.platform, lt.listing_id "
            "FROM listing_card_assignments lca "
            "JOIN listing_templates lt ON lt.id = lca.template_id "
            "WHERE lca.id = %s",
            (row_id,),
        )
        roster_row = cur.fetchone()
        if roster_row is None:
            return {"row_id": row_id, "pushed": False, "dry_run": dry_run, "error": "no such roster row"}
        if roster_row["status"] != "queued":
            return {"row_id": row_id, "pushed": False, "dry_run": dry_run,
                     "error": f"row is {roster_row['status']!r}, not 'queued' — nothing to push live"}

        listing_id = roster_row["listing_id"]
        row_platform = roster_row["platform"]
        template = _resolve_template(cur, row_platform, listing_id)
        if template is None:
            return {"row_id": row_id, "pushed": False, "dry_run": dry_run,
                     "error": f"no listing_templates row for {row_platform} listing {listing_id}"}

        cur.execute("SELECT * FROM resolve_listing_prices(%s, %s)", (row_platform, listing_id))
        resolved_by_row_id = {r["row_id"]: r for r in cur.fetchall()}
        promoted_resolved = resolved_by_row_id.get(row_id)
        if promoted_resolved is None:
            return {"row_id": row_id, "pushed": False, "dry_run": dry_run,
                     "error": "row not found in resolve_listing_prices() output"}

        # eBay silently drops any variation added at quantity 0 — it
        # returns Ack=Warning (not Failure), with "Variations with
        # quantity '0' will be removed" buried in the Errors block, which
        # nothing here inspects. Without this check we'd record a
        # confident "pushed live" in our own DB for a variation that
        # never actually existed on eBay — confirmed live, 2026-08.
        # available_qty is the ceiling every downstream qty_to_push
        # computation in _stage_promotion() only ever narrows further
        # (low_stock/quantity_limit both only reduce), so if it's already
        # 0 there's no path to a nonzero push regardless of those.
        if not promoted_resolved["available_qty"]:
            return {"row_id": row_id, "pushed": False, "dry_run": dry_run,
                     "error": "0 available quantity — eBay silently rejects a new variation "
                              "added at quantity 0, so there's nothing to push until this card "
                              "has real inventory"}

        item = fetch_item(listing_id, account_num=account_num)
        variations_node = _find(item, "Variations")
        if variations_node is None:
            return {"row_id": row_id, "pushed": False, "dry_run": dry_run,
                     "error": "live listing has no <Variations> block — not a multi-variation listing"}

        variations = deep_copy_variations(item)
        normalize_quantities(variations)
        strip_selling_status(variations)

        live_variation_count = len(_findall(variations, "Variation"))
        if live_variation_count >= 250:
            return {"row_id": row_id, "pushed": False, "dry_run": dry_run,
                     "error": f"listing already has {live_variation_count} live variations "
                              f"(eBay's hard cap is 250) — free a slot first, e.g. via the general "
                              f"Push button, which swaps out a sold-out row"}

        specifics_set = get_specifics_set(variations)
        specific_name = next(iter(specifics_set), None)
        if specific_name is None:
            return {"row_id": row_id, "pushed": False, "dry_run": dry_run,
                     "error": "listing has no VariationSpecificsSet to add a value to"}

        cur.execute(
            "SELECT DISTINCT account FROM platform_listings "
            "WHERE platform = %s AND listing_id = %s AND account IS NOT NULL LIMIT 1",
            (row_platform, listing_id),
        )
        acct_row = cur.fetchone()
        account = acct_row["account"] if acct_row else None

        promotion, pending_writes = _stage_promotion(
            cur, template, row_platform, listing_id,
            {"id": row_id, "variant_id": roster_row["variant_id"], "custom_name": roster_row["custom_name"],
             "eps_picture_url": roster_row["eps_picture_url"], "card_photo_id": roster_row["card_photo_id"]},
            promoted_resolved, variations, specific_name,
            template.get("display_sort") or "card_number", account,
        )
        photo_urls = resolve_photo_urls(cur, promotion)
        if photo_urls:
            set_variation_picture(variations, promotion["external_id"], photo_urls)

        if dry_run:
            p(f"[DRY-RUN] would push {promotion['external_id']!r} live on {listing_id}: "
              f"${promotion['resolved_price']:.2f}, qty {promotion['qty_to_push']}")
            return {**promotion, "pushed": False, "dry_run": True}

        xml = build_revise_xml(listing_id, variations, "ReviseFixedPriceItem", account_num=account_num)
        _post("ReviseFixedPriceItem", xml, account_num=account_num)

        for sql, params in pending_writes:
            cur.execute(sql, params)

        p(f"[{listing_id}] pushed {promotion['external_id']!r} live: "
          f"${promotion['resolved_price']:.2f}, qty {promotion['qty_to_push']}")
        return {**promotion, "pushed": True, "dry_run": False}


def remove_single_card_live(row_id: str, account_num: int = 1, platform: str = "ebay",
                             dry_run: bool = False, quiet: bool = False) -> dict:
    """
    Pulls ONE active roster row's variation off its live eBay listing —
    the reverse of push_single_card_live(). Deletes only that one
    <Variation> (via mark_variation_deleted, same helper the general
    push's 250-cap swap uses to free a slot) — every other variation's
    XML is untouched. On success the roster row goes back to 'queued'
    (platform_listing_id cleared) rather than being deleted outright, so
    it can be pushed live again later with no extra setup; the old
    platform_listings row is kept as history (status='delisted',
    sync_enabled=false) instead of deleted.
    """
    def p(msg):
        if not quiet:
            print(msg)

    with db_cursor() as cur:
        cur.execute(
            "SELECT lca.id, lca.platform_listing_id, lca.status, lt.platform, lt.listing_id "
            "FROM listing_card_assignments lca "
            "JOIN listing_templates lt ON lt.id = lca.template_id "
            "WHERE lca.id = %s",
            (row_id,),
        )
        roster_row = cur.fetchone()
        if roster_row is None:
            return {"row_id": row_id, "removed": False, "dry_run": dry_run, "error": "no such roster row"}
        if roster_row["status"] != "active" or not roster_row["platform_listing_id"]:
            return {"row_id": row_id, "removed": False, "dry_run": dry_run,
                     "error": f"row is {roster_row['status']!r}, not 'active' — nothing live to remove"}

        listing_id = roster_row["listing_id"]
        row_platform = roster_row["platform"]
        old_platform_listing_id = roster_row["platform_listing_id"]

        cur.execute(
            "SELECT external_id FROM platform_listings WHERE id = %s",
            (old_platform_listing_id,),
        )
        pl_row = cur.fetchone()
        if pl_row is None or not pl_row["external_id"]:
            return {"row_id": row_id, "removed": False, "dry_run": dry_run,
                     "error": "no external_id on the platform_listings row — can't locate the live variation"}
        external_id = pl_row["external_id"]

        item = fetch_item(listing_id, account_num=account_num)
        variations_node = _find(item, "Variations")
        if variations_node is None:
            return {"row_id": row_id, "removed": False, "dry_run": dry_run,
                     "error": "live listing has no <Variations> block — not a multi-variation listing"}

        variations = deep_copy_variations(item)
        normalize_quantities(variations)
        strip_selling_status(variations)

        specifics_set = get_specifics_set(variations)
        specific_name = next(iter(specifics_set), None)
        var_el = find_variation_by_specifics(variations, specific_name, external_id) if specific_name else None
        if var_el is None:
            return {"row_id": row_id, "removed": False, "dry_run": dry_run,
                     "error": f"variation {external_id!r} not found live — mismatch, needs manual reconcile in Seller Hub"}

        mark_variation_deleted(var_el)

        if dry_run:
            p(f"[DRY-RUN] would remove {external_id!r} from {listing_id} — roster row goes back to 'queued'")
            return {"row_id": row_id, "external_id": external_id, "removed": False, "dry_run": True}

        xml = build_revise_xml(listing_id, variations, "ReviseFixedPriceItem", account_num=account_num)
        _post("ReviseFixedPriceItem", xml, account_num=account_num)

        cur.execute(
            "UPDATE platform_listings SET status = 'delisted', sync_enabled = false WHERE id = %s",
            (old_platform_listing_id,),
        )
        cur.execute(
            "UPDATE listing_card_assignments SET status = 'queued', platform_listing_id = NULL, updated_at = now() "
            "WHERE id = %s",
            (row_id,),
        )

        p(f"[{listing_id}] removed {external_id!r} — roster row back to 'queued'")
        return {"row_id": row_id, "external_id": external_id, "removed": True, "dry_run": False}


def push_card_photo_live(row_id: str, account_num: int = 1, dry_run: bool = False,
                          quiet: bool = False) -> dict:
    """
    Revises ONLY the <VariationSpecificPictureSet> entry for one already-
    live variation — no other variation's price/qty/picture is touched,
    and this variation's own price/qty are untouched too. This is the
    active-row counterpart to assign_card_photo(): reassigning
    card_photo_id alone only changes what the roster THINKS this card's
    picture is; nothing reaches eBay until this is called (mirrors how
    stage_card_picture() for a queued row doesn't go live until
    push_single_card_live()/promotion picks it up).

    Resolves the picture set from the roster row's CURRENT card_photo_id/
    eps_picture_url via resolve_photo_urls() — always the live DB state,
    not a value passed in, so this can't drift from what "Manage photos"
    last saved.
    """
    def p(msg):
        if not quiet:
            print(msg)

    with db_cursor() as cur:
        cur.execute(
            "SELECT lca.id, lca.platform_listing_id, lca.status, lca.eps_picture_url, lca.card_photo_id, "
            "lt.platform, lt.listing_id "
            "FROM listing_card_assignments lca "
            "JOIN listing_templates lt ON lt.id = lca.template_id "
            "WHERE lca.id = %s",
            (row_id,),
        )
        roster_row = cur.fetchone()
        if roster_row is None:
            return {"row_id": row_id, "pushed": False, "dry_run": dry_run, "error": "no such roster row"}
        if roster_row["status"] != "active" or not roster_row["platform_listing_id"]:
            return {"row_id": row_id, "pushed": False, "dry_run": dry_run,
                     "error": f"row is {roster_row['status']!r}, not 'active' — nothing live to update"}

        listing_id = roster_row["listing_id"]
        platform_listing_id = roster_row["platform_listing_id"]

        cur.execute(
            "SELECT external_id FROM platform_listings WHERE id = %s",
            (platform_listing_id,),
        )
        pl_row = cur.fetchone()
        if pl_row is None or not pl_row["external_id"]:
            return {"row_id": row_id, "pushed": False, "dry_run": dry_run,
                     "error": "no external_id on the platform_listings row — can't locate the live variation"}
        external_id = pl_row["external_id"]

        photo_urls = resolve_photo_urls(cur, roster_row)
        if not photo_urls:
            return {"row_id": row_id, "pushed": False, "dry_run": dry_run,
                     "error": "no card_photo_id or eps_picture_url set on this row — nothing to push"}

        item = fetch_item(listing_id, account_num=account_num)
        variations_node = _find(item, "Variations")
        if variations_node is None:
            return {"row_id": row_id, "pushed": False, "dry_run": dry_run,
                     "error": "live listing has no <Variations> block — not a multi-variation listing"}

        variations = deep_copy_variations(item)
        normalize_quantities(variations)
        strip_selling_status(variations)

        specifics_set = get_specifics_set(variations)
        specific_name = next(iter(specifics_set), None)
        var_el = find_variation_by_specifics(variations, specific_name, external_id) if specific_name else None
        if var_el is None:
            return {"row_id": row_id, "pushed": False, "dry_run": dry_run,
                     "error": f"variation {external_id!r} not found live — mismatch, needs manual reconcile in Seller Hub"}

        set_variation_picture(variations, external_id, photo_urls)

        if dry_run:
            p(f"[DRY-RUN] would push {len(photo_urls)} picture(s) live for {external_id!r} on {listing_id}")
            return {"row_id": row_id, "external_id": external_id, "photo_urls": photo_urls,
                     "pushed": False, "dry_run": True}

        xml = build_revise_xml(listing_id, variations, "ReviseFixedPriceItem", account_num=account_num)
        _post("ReviseFixedPriceItem", xml, account_num=account_num)

        p(f"[{listing_id}] pushed {len(photo_urls)} picture(s) live for {external_id!r}")
        return {"row_id": row_id, "external_id": external_id, "photo_urls": photo_urls,
                 "pushed": True, "dry_run": False}


def stage_card_picture(row_id: str, source_url: str = None, image_bytes: bytes = None,
                        filename: str = None, account_num: int = 1, quiet: bool = False) -> dict:
    """
    Uploads a picture to eBay's EPS right now and stores the resulting
    hosted URL on a QUEUED roster row (listing_card_assignments.
    eps_picture_url) — nothing changes on the live listing yet, since a
    queued card has no live variation to attach a picture to. The staged
    URL rides along automatically the next time this row actually gets
    pushed live (push_single_card_live, or the general push's promotion
    path) — see set_variation_picture() in ebay_variations_xml.py.

    Pass either `source_url` (fetched, then uploaded) or
    `image_bytes`+`filename` (uploaded directly, e.g. a browser file
    upload) — not both. Only ever offered for queued rows: an
    already-active row has nothing here to stage against (see
    docs/plans/listing-pricing-system.md).
    """
    def p(msg):
        if not quiet:
            print(msg)

    if not source_url and not image_bytes:
        return {"row_id": row_id, "staged": False, "error": "must provide source_url or image_bytes"}

    with db_cursor() as cur:
        cur.execute("SELECT id, status FROM listing_card_assignments WHERE id = %s", (row_id,))
        roster_row = cur.fetchone()
        if roster_row is None:
            return {"row_id": row_id, "staged": False, "error": "no such roster row"}
        if roster_row["status"] != "queued":
            return {"row_id": row_id, "staged": False,
                     "error": f"row is {roster_row['status']!r}, not 'queued' — pictures can only be "
                              f"staged for queued cards right now"}

        try:
            if source_url:
                eps_url = upload_picture_from_url(source_url, account_num=account_num)
            else:
                eps_url = upload_picture_bytes(image_bytes, filename or "card.jpg", account_num=account_num)
        except Exception as e:
            return {"row_id": row_id, "staged": False, "error": f"EPS upload failed: {e}"}

        cur.execute(
            "UPDATE listing_card_assignments SET eps_picture_url = %s, updated_at = now() WHERE id = %s",
            (eps_url, row_id),
        )

    p(f"Staged picture for row {row_id}: {eps_url}")
    return {"row_id": row_id, "staged": True, "eps_picture_url": eps_url}


def stage_nav_image(template_id: str, source_url: str = None, image_bytes: bytes = None,
                     filename: str = None, account_num: int = 1, quiet: bool = False) -> dict:
    """
    Uploads a picture to eBay's EPS right now and writes the resulting
    hosted URL straight onto listing_templates.nav_image_url — takes
    effect immediately (unlike stage_card_picture's queued-row staging,
    nav_image_url isn't tied to a push; it's read directly by
    render_description()'s family_nav/era_nav modules on every render).

    Pass either `source_url` (fetched, then uploaded) or
    `image_bytes`+`filename` (uploaded directly, e.g. a browser file
    upload) — not both.
    """
    def p(msg):
        if not quiet:
            print(msg)

    if not source_url and not image_bytes:
        return {"template_id": template_id, "staged": False, "error": "must provide source_url or image_bytes"}

    with db_cursor() as cur:
        cur.execute("SELECT id FROM listing_templates WHERE id = %s", (template_id,))
        if cur.fetchone() is None:
            return {"template_id": template_id, "staged": False, "error": "no such listing template"}

        try:
            if source_url:
                eps_url = upload_picture_from_url(source_url, account_num=account_num)
            else:
                eps_url = upload_picture_bytes(image_bytes, filename or "nav.jpg", account_num=account_num)
        except Exception as e:
            return {"template_id": template_id, "staged": False, "error": f"EPS upload failed: {e}"}

        cur.execute(
            "UPDATE listing_templates SET nav_image_url = %s, updated_at = now() WHERE id = %s",
            (eps_url, template_id),
        )

    p(f"Staged nav image for template {template_id}: {eps_url}")
    return {"template_id": template_id, "staged": True, "nav_image_url": eps_url}


def revise_single_variation_qty(platform_listing_id: str, new_qty: int, account_num: int = 1,
                                 dry_run: bool = False, quiet: bool = False) -> dict:
    """
    Directly revises ONE existing live variation's quantity — price
    untouched, only <Quantity>. Deliberately works whether or not the
    listing has a listing_templates row: everything needed
    (listing_id, external_id, account, platform) already lives on the
    platform_listings row itself, so this never touches
    listing_card_assignments/resolve_listing_prices() at all. Built for
    "Balance Qty" — redistributing a card's shared inventory across
    every listing that currently offers it, including ones never
    onboarded into a template.
    """
    def p(msg):
        if not quiet:
            print(msg)

    if new_qty < 0:
        return {"platform_listing_id": platform_listing_id, "revised": False, "dry_run": dry_run,
                 "error": "quantity can't be negative"}

    with db_cursor() as cur:
        cur.execute(
            "SELECT id, platform, listing_id, external_id, account, quantity_listed "
            "FROM platform_listings WHERE id = %s",
            (platform_listing_id,),
        )
        pl_row = cur.fetchone()
        if pl_row is None:
            return {"platform_listing_id": platform_listing_id, "revised": False, "dry_run": dry_run,
                     "error": "no such platform_listings row"}
        if not pl_row["external_id"]:
            return {"platform_listing_id": platform_listing_id, "revised": False, "dry_run": dry_run,
                     "error": "no external_id on this row — can't locate the live variation"}

        listing_id = pl_row["listing_id"]
        external_id = pl_row["external_id"]

        item = fetch_item(listing_id, account_num=account_num)
        variations_node = _find(item, "Variations")
        if variations_node is None:
            return {"platform_listing_id": platform_listing_id, "revised": False, "dry_run": dry_run,
                     "error": "live listing has no <Variations> block — not a multi-variation listing"}

        variations = deep_copy_variations(item)
        normalize_quantities(variations)
        strip_selling_status(variations)

        specifics_set = get_specifics_set(variations)
        specific_name = next(iter(specifics_set), None)
        var_el = find_variation_by_specifics(variations, specific_name, external_id) if specific_name else None
        if var_el is None:
            return {"platform_listing_id": platform_listing_id, "revised": False, "dry_run": dry_run,
                     "error": f"variation {external_id!r} not found live — mismatch, needs manual reconcile in Seller Hub"}

        if dry_run:
            p(f"[DRY-RUN] would revise {external_id!r} on {listing_id}: "
              f"qty {pl_row['quantity_listed']} -> {new_qty}")
            return {"platform_listing_id": platform_listing_id, "external_id": external_id,
                     "old_qty": pl_row["quantity_listed"], "new_qty": new_qty,
                     "revised": False, "dry_run": True}

        set_variation_price_qty(var_el, quantity=new_qty)

        xml = build_revise_xml(listing_id, variations, "ReviseFixedPriceItem", account_num=account_num)
        _post("ReviseFixedPriceItem", xml, account_num=account_num)

        # status must track new_qty (same active/out_of_stock convention as
        # every other writer of this column — push_staging_row_to_inventory,
        # _stage_promotion) — resolve_listing_prices()'s shared-inventory
        # subtraction only counts an OTHER listing's claimed quantity when
        # its status='active', so leaving a stale 'out_of_stock' here after
        # reviving it with real stock would make every other listing
        # sharing this variant silently overcount what's actually available.
        cur.execute(
            "UPDATE platform_listings SET quantity_listed = %s, pushed_qty = %s, pushed_at = now(), "
            "status = %s WHERE id = %s",
            (new_qty, new_qty, 'active' if new_qty > 0 else 'out_of_stock', platform_listing_id),
        )

    p(f"[{listing_id}] revised {external_id!r} quantity {pl_row['quantity_listed']} -> {new_qty}")
    return {"platform_listing_id": platform_listing_id, "external_id": external_id,
            "old_qty": pl_row["quantity_listed"], "new_qty": new_qty, "revised": True, "dry_run": False}


# ══════════════════════════════════════════════════════════════════════════════
# One-time (re-runnable) adoption of variations live on eBay that this app
# never tracked at all
# ══════════════════════════════════════════════════════════════════════════════

def adopt_untracked_live_variations(listing_id: str, account_num: int = 1,
                                     platform: str = "ebay", dry_run: bool = False) -> dict:
    """Some live listings have real <Variation> entries on eBay with NO
    corresponding platform_listings row at all — confirmed 2026-08-17 on
    the "Chaos Rising Reverse Holo - Ultra Rare" listing (125 live
    variations, only 105 tracked). These weren't sold out and cleaned
    up by this app (that path clears platform_listing_id but keeps the
    listing_card_assignments row as 'sold_out_retained' — none existed
    here either); they were added directly through eBay's own tools,
    same "attached outside anything this codebase tracked" pattern as
    the card_photos backfill. Fei's complaint: without a roster row,
    "Add card to listing" has no way to know the card is already live,
    so adding it again would create a real duplicate variation on eBay
    instead of recognizing the existing one.

    Reuses the exact same fetch+parse+match pipeline as --ebay-import
    (fetch_item_variations() -> parse_variation_name() ->
    lookup_card_for_ebay(), importer/ebay.py / utils/pokemon_api.py) --
    the same matcher every real purchase already trusts -- rather than
    a second, less-trusted variant-matching path. Only variations whose
    exact eBay variation text isn't already a platform_listings.external_id
    for this listing_id are considered; already-tracked ones are left
    alone entirely (no re-matching, no risk of overwriting a correct
    existing row).

    For each matched, previously-untracked variation: inserts a
    platform_listings row (status 'active'/'out_of_stock' by current
    live quantity, pushed_price/pushed_qty snapshotting what's already
    live -- no eBay Revise call needed, it's already there) + a
    listing_card_assignments row pointing at it + an ebay_listing_map
    upsert (item_id, variation_name) -> variant_id, same three writes
    _stage_promotion() makes when promoting a card live -- without this
    last one, importer/ebay_orders.py's sale matcher would never learn
    these variants sell on this listing_id, and a real sale would show
    up "unmatched" even though the roster row is now correct.

    dry_run=True runs the fetch + match but writes nothing.
    """
    from importer.ebay import fetch_item_variations
    from utils.pokemon_api import lookup_card_for_ebay

    with db_cursor() as cur:
        cur.execute(
            "SELECT id FROM listing_templates WHERE listing_id = %s AND platform = %s",
            (listing_id, platform),
        )
        template = cur.fetchone()
        if template is None:
            return {"error": f"no listing_templates row for listing_id={listing_id!r}"}
        template_id = template["id"]

        cur.execute("SELECT external_id FROM platform_listings WHERE listing_id = %s", (listing_id,))
        tracked = set(r["external_id"] for r in cur.fetchall())

        cur.execute(
            "SELECT account FROM platform_listings WHERE listing_id = %s AND account IS NOT NULL LIMIT 1",
            (listing_id,),
        )
        acct_row = cur.fetchone()
        account = acct_row["account"] if acct_row else get_account_name(account_num)

        cur.execute(
            "SELECT COALESCE(MAX(priority_rank), -1) + 1 AS next_rank "
            "FROM listing_card_assignments WHERE template_id = %s",
            (template_id,),
        )
        next_rank = cur.fetchone()["next_rank"]

    rows = fetch_item_variations(listing_id, title="", account_num=account_num)
    untracked = [r for r in rows if r.get("variation_name") and r["variation_name"] not in tracked]

    adopted, unmatched, errors, duplicate_variant = [], [], [], []
    for r in untracked:
        card_name   = r.get("card_name") or r.get("variation_name", "")
        card_number = r.get("card_number", "")
        set_name    = r.get("set_override") or r.get("set_name", "")

        try:
            lookup = lookup_card_for_ebay(
                card_name=card_name, card_number=card_number, set_name=set_name,
                foil_type=r.get("foil_type"), foil_pattern=r.get("foil_pattern"),
                texture=r.get("texture"), material=r.get("material"), size=r.get("size"),
                stamp_type=r.get("stamp_type"), source_type=r.get("source_type"),
            )
        except Exception as e:
            errors.append({"variation_name": r["variation_name"], "error": str(e)})
            continue

        if not lookup["matched"]:
            unmatched.append({"variation_name": r["variation_name"], "card_name": card_name,
                               "card_number": card_number, "set_name": set_name})
            continue

        if dry_run:
            adopted.append({"variation_name": r["variation_name"], "variant_id": lookup["variant_id"],
                             "card_name": lookup["card_name"], "quantity": r["quantity"],
                             "price": r["price"], "dry_run": True})
            continue

        status = "active" if r["quantity"] > 0 else "out_of_stock"

        # This exact (platform, account, listing_id, variant_id) can already
        # exist under a DIFFERENT external_id — either a real eBay rename/
        # duplicate-text artifact, or (observed in practice) a race when two
        # adoption runs process overlapping listings concurrently and one
        # commits the row between the other's "already tracked" snapshot and
        # its INSERT. Either way there's nothing to adopt: this variant is
        # already on the roster here. Checking first (rather than catching
        # the resulting IntegrityError) keeps one bad/racy row from aborting
        # the whole listing's remaining untracked cards.
        with db_cursor() as cur:
            cur.execute(
                "SELECT external_id FROM platform_listings "
                "WHERE platform = %s AND account = %s AND listing_id = %s AND variant_id = %s",
                (platform, account, listing_id, lookup["variant_id"]),
            )
            existing = cur.fetchone()
        if existing:
            duplicate_variant.append({"variation_name": r["variation_name"],
                                       "already_tracked_as": existing["external_id"],
                                       "variant_id": lookup["variant_id"]})
            continue

        new_platform_listing_id = str(uuid.uuid4())
        with db_cursor() as cur:
            cur.execute(
                """
                INSERT INTO platform_listings
                    (id, platform, account, listing_id, external_id, variant_id, list_price,
                     quantity_listed, status, sync_enabled, template_id, listed_at,
                     pushed_price, pushed_qty, pushed_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, true, %s, now(), %s, %s, now())
                """,
                (new_platform_listing_id, platform, account, listing_id, r["variation_name"],
                 lookup["variant_id"], r["price"], r["quantity"], status, template_id,
                 r["price"], r["quantity"]),
            )
            cur.execute(
                """
                INSERT INTO listing_card_assignments
                    (template_id, variant_id, platform_listing_id, status, priority_rank)
                VALUES (%s, %s, %s, 'active', %s)
                """,
                (template_id, lookup["variant_id"], new_platform_listing_id, next_rank),
            )
            cur.execute(
                """
                INSERT INTO ebay_listing_map
                    (item_id, listing_id, variation_name, variant_id, condition, source, last_synced_at)
                VALUES (%s, %s, %s, %s, 'Near Mint', 'adopt', now())
                ON CONFLICT (item_id, variation_name) DO UPDATE
                    SET variant_id = EXCLUDED.variant_id,
                        condition = EXCLUDED.condition,
                        source = EXCLUDED.source,
                        last_synced_at = now()
                """,
                (listing_id, listing_id, r["variation_name"], lookup["variant_id"]),
            )
        next_rank += 1
        adopted.append({"variation_name": r["variation_name"], "variant_id": lookup["variant_id"],
                         "card_name": lookup["card_name"], "platform_listing_id": new_platform_listing_id,
                         "status": status, "quantity": r["quantity"], "price": r["price"]})

    return {"checked": len(untracked), "adopted": adopted, "unmatched": unmatched, "errors": errors,
             "duplicate_variant": duplicate_variant}
