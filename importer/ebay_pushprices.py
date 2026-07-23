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

from db.connection import db_cursor
from importer.ebay_auth import get_account_name, get_user_token
from importer.ebay import _post, _find, _findall
from importer.ebay_variations_xml import (
    fetch_item, deep_copy_variations, strip_selling_status, get_quantity_sold,
    find_variation_by_specifics, set_variation_price_qty, get_specifics_set,
    insert_specifics_value, mark_variation_deleted, add_variation_row,
    build_revise_xml,
)
from importer.ebay_listing_sync import (
    platform_sync_allowed, _render_variation_name, _compute_insert_position,
)


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
    listing_card_assignments.id, and only rows with status='active' (a
    live platform_listings row) are ever eligible for `changes` — queued
    rows show up in `resolved` for preview but are never pushed directly
    (only relevant via 250-cap promotion).
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

        price_changed = (
            cur_row["pushed_price"] is None
            or abs(float(cur_row["pushed_price"]) - float(r["resolved_price"])) >= 0.005
        )
        qty_changed = cur_row["pushed_qty"] is None or cur_row["pushed_qty"] != qty_to_push
        if not (price_changed or qty_changed):
            continue

        change = {
            "row_id": r["row_id"],
            "platform_listing_id": r["platform_listing_id"],
            "external_id": cur_row["external_id"],
            "derived_label": r["derived_label"],
            "resolved_price": float(r["resolved_price"]),
            "price_source": r["price_source"],
            "qty_to_push": qty_to_push,
        }

        gated_in = (
            cur_row["sync_enabled"]
            and cur_row["status"] == "active"
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


def _do_promotions(cur, template, platform: str, listing_id: str, account_num: int,
                    resolved: list, variations, quantity_sold_by_var: dict, quiet: bool):
    """
    250-cap promotion: if the roster (all statuses) exceeds 250 and a live
    'active' row is sold out (available_qty <= 0), delete its variation
    and promote the highest-priority 'queued' row into the freed slot —
    creates a NEW platform_listings row for it (inherits sync_enabled=true
    since the listing it's joining is already being pushed) and updates
    listing_card_assignments. Mutates `variations` in place. Returns a
    list of "change"-shaped dicts for the newly promoted rows (to be
    pushed in the same Revise call and stamped afterward), and the
    account name to use for the new platform_listings rows.
    """
    def p(msg):
        if not quiet:
            print(msg)

    cur.execute(
        "SELECT COUNT(*) AS n FROM listing_card_assignments WHERE template_id = %s",
        (template["id"],),
    )
    total_roster = cur.fetchone()["n"]
    if total_roster <= 250:
        return []

    cur.execute(
        "SELECT id, variant_id, priority_rank FROM listing_card_assignments "
        "WHERE template_id = %s AND status = 'queued' ORDER BY priority_rank ASC",
        (template["id"],),
    )
    queued = cur.fetchall()
    if not queued:
        return []
    queued = list(queued)

    resolved_by_row_id = {r["row_id"]: r for r in resolved}
    active_rows_sql = [r for r in resolved if r["status"] == "active" and r["platform_listing_id"]]

    cur.execute(
        "SELECT id, external_id, account FROM platform_listings WHERE id = ANY(%s::uuid[])",
        ([str(r["platform_listing_id"]) for r in active_rows_sql],),
    )
    listing_meta = {r["id"]: r for r in cur.fetchall()}

    specifics_set = get_specifics_set(variations)
    specific_name = next(iter(specifics_set), None)
    display_sort = template.get("display_sort") or "card_number"

    promotions = []
    for r in active_rows_sql:
        if not queued:
            break
        if (r["available_qty"] or 0) > 0:
            continue  # not sold out, never delete a row with stock

        meta = listing_meta.get(r["platform_listing_id"])
        if not meta or specific_name is None:
            continue
        var_el = find_variation_by_specifics(variations, specific_name, meta["external_id"])
        if var_el is None:
            continue

        promote = queued.pop(0)
        mark_variation_deleted(var_el)

        promoted_name = _render_variation_name(cur, promote["variant_id"], template["id"])
        position = _compute_insert_position(cur, variations, specific_name, listing_id,
                                             promote["variant_id"], display_sort)
        insert_specifics_value(variations, specific_name, promoted_name, position=position)

        promoted_resolved = resolved_by_row_id.get(promote["id"])
        resolved_price = float(promoted_resolved["resolved_price"]) if promoted_resolved else 0.0
        available = (promoted_resolved["available_qty"] or 0) if promoted_resolved else 0
        low_stock = promoted_resolved["low_stock_qty"] if promoted_resolved else None
        qty_limit = promoted_resolved["quantity_limit"] if promoted_resolved else None
        qty_to_push = max(available - low_stock, 0) if low_stock is not None else available
        if qty_limit is not None:
            qty_to_push = min(qty_to_push, qty_limit)

        add_variation_row(variations, {specific_name: promoted_name}, quantity=qty_to_push,
                           start_price=resolved_price)

        cur.execute(
            """
            INSERT INTO platform_listings
                (platform, account, listing_id, external_id, variant_id, list_price,
                 quantity_listed, status, sync_enabled, template_id, listed_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'active', true, %s, now())
            RETURNING id
            """,
            (platform, meta["account"], listing_id, promoted_name, promote["variant_id"],
             resolved_price, qty_to_push, template["id"]),
        )
        new_platform_listing_id = cur.fetchone()["id"]

        cur.execute(
            "UPDATE listing_card_assignments SET status = 'sold_out_retained', updated_at = now() WHERE id = %s",
            (r["row_id"],),
        )
        cur.execute(
            "UPDATE listing_card_assignments SET status = 'active', platform_listing_id = %s, updated_at = now() "
            "WHERE id = %s",
            (new_platform_listing_id, promote["id"]),
        )

        promotions.append({
            "row_id": promote["id"],
            "platform_listing_id": new_platform_listing_id,
            "external_id": promoted_name,
            "derived_label": promoted_resolved["derived_label"] if promoted_resolved else promoted_name,
            "resolved_price": resolved_price,
            "price_source": promoted_resolved["price_source"] if promoted_resolved else "default",
            "qty_to_push": qty_to_push,
        })
        p(f"    [PROMOTE] {meta['external_id']} (sold out) -> {promoted_name!r} at position {position if position is not None else 'end'}")

    return promotions


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
            return {"listing_id": listing_id, "resolved": 0, "changed": 0, "pushed": 0,
                    "warnings": ["no template for this listing_id"], "dry_run": dry_run}

        resolved, changes, skipped_ungated = _compute_roster_changes(cur, platform, listing_id)
        summary = {"listing_id": listing_id, "resolved": len(resolved), "changed": len(changes),
                   "pushed": 0, "warnings": [], "dry_run": dry_run}

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
            item = fetch_item(listing_id, account_num=account_num)
            qty_sold = get_quantity_sold(item)
            qty_to_set = qty_sold + change["qty_to_push"]

            p(f"  [single] {change['derived_label']}: ${change['resolved_price']:.2f}, "
              f"qty -> {qty_to_set} (sold={qty_sold} + push={change['qty_to_push']}) [{change['price_source']}]")

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
    <Quantity>{qty_to_set}</Quantity>
  </Item>
</ReviseFixedPriceItemRequest>"""
            _post("ReviseFixedPriceItem", xml, account_num=account_num)
            cur.execute(
                "UPDATE platform_listings SET pushed_price = %s, pushed_qty = %s, pushed_at = now() WHERE id = %s",
                (change["resolved_price"], change["qty_to_push"], change["platform_listing_id"]),
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
        quantity_sold_by_var = {id(v): get_quantity_sold(v) for v in _findall(variations, "Variation")}
        strip_selling_status(variations)

        specifics_set = get_specifics_set(variations)
        specific_name = next(iter(specifics_set), None)

        pushed = []
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

            qty_sold = quantity_sold_by_var.get(id(var_el), 0)
            qty_to_set = qty_sold + change["qty_to_push"]
            set_variation_price_qty(var_el, start_price=change["resolved_price"], quantity=qty_to_set)
            pushed.append(change)
            p(f"  {change['external_id']}: ${change['resolved_price']:.2f}, qty -> {qty_to_set} "
              f"(sold={qty_sold} + push={change['qty_to_push']}) [{change['price_source']}]")

        promotions = _do_promotions(cur, template, platform, listing_id, account_num,
                                     resolved, variations, quantity_sold_by_var, quiet)
        pushed.extend(promotions)
        for promo in promotions:
            p(f"  {promo['external_id']}: ${promo['resolved_price']:.2f}, qty -> {promo['qty_to_push']} "
              f"(newly promoted) [{promo['price_source']}]")

        if not pushed:
            p(f"[{listing_id}] {len(changes)} change(s) computed, but none matched a live variation "
              f"and no promotions triggered — nothing pushed.")
            return summary

        if dry_run:
            p(f"[DRY-RUN] would push {len(pushed)} of {len(resolved)} row(s) to {listing_id} "
              f"({len(promotions)} via 250-cap promotion) — not re-sending all {len(resolved)}")
            summary["pushed"] = len(pushed)
            return summary

        xml = build_revise_xml(listing_id, variations, "ReviseFixedPriceItem", account_num=account_num)
        _post("ReviseFixedPriceItem", xml, account_num=account_num)

        for change in pushed:
            cur.execute(
                "UPDATE platform_listings SET pushed_price = %s, pushed_qty = %s, pushed_at = now() WHERE id = %s",
                (change["resolved_price"], change["qty_to_push"], change["platform_listing_id"]),
            )

        summary["pushed"] = len(pushed)
        p(f"[{listing_id}] pushed {len(pushed)} of {len(resolved)} row(s) ({len(promotions)} promoted).")
        return summary
