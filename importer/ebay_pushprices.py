"""
importer/ebay_pushprices.py — pushes resolved prices/quantities for one
eBay listing via the new Listing Pricing System
(docs/plans/listing-pricing-system.md), which replaces the
card_type_mapping / price_tiers-as-global pipeline in
importer/ebay_listing_sync.py.

Resolution (price, quantity, source) always comes from the
resolve_listing_prices(platform, listing_id) Postgres RPC — never
recomputed here — so the web pricing grid and this CLI job can never
disagree. This module's only job is: diff resolved vs. last-pushed state,
send ONLY the changed variations to eBay, and record what was pushed.

Quantity gating: available_qty is reduced by low_stock_qty (floored at 0)
before being sent, per the spec's recommendation (Open Question 2) — this
holds back low_stock_qty units from ever appearing as purchasable on
eBay, rather than just warning about it.
"""

from db.connection import db_cursor
from importer.ebay_auth import get_account_name, get_user_token
from importer.ebay import _post, _find, _findall
from importer.ebay_variations_xml import (
    fetch_item, deep_copy_variations, strip_selling_status, get_quantity_sold,
    find_variation_by_specifics, set_variation_price_qty, get_specifics_set,
    build_revise_xml,
)


def _compute_changes(cur, platform: str, listing_id: str):
    """
    Returns (resolved_rows, changes, skipped_ungated) where:
    - resolved_rows: raw resolve_listing_prices() output, unfiltered (used
      for display/preview — a row not yet opted into sync should still be
      visible with its computed price, just never pushed).
    - changes: rows that are both GATED-IN (sync_enabled + status='active'
      + no kill switch engaged, same rules as ebay_listing_sync.py's
      _resolve_scope) and have a resolved price/qty that differs from
      what was last pushed.
    - skipped_ungated: rows that would otherwise be changes but aren't
      gated in — surfaced so it's obvious *why* a row didn't push, rather
      than silently dropping it.
    """
    cur.execute("SELECT * FROM resolve_listing_prices(%s, %s)", (platform, listing_id))
    resolved = cur.fetchall()
    if not resolved:
        return [], [], []

    row_ids = [str(r["row_id"]) for r in resolved]
    cur.execute(
        "SELECT id, external_id, pushed_price, pushed_qty, sync_enabled, status, account "
        "FROM platform_listings WHERE id = ANY(%s::uuid[])",
        (row_ids,),
    )
    current_by_id = {r["id"]: r for r in cur.fetchall()}

    # Kill-switch check per distinct account present (usually just one —
    # all variations of one eBay listing normally belong to one account —
    # but don't assume it).
    from importer.ebay_listing_sync import platform_sync_allowed
    accounts_present = {r["account"] for r in current_by_id.values()} | {None}
    kill_switch_ok = {a: platform_sync_allowed(cur, platform, a) for a in accounts_present}

    changes = []
    skipped_ungated = []
    for r in resolved:
        cur_row = current_by_id.get(r["row_id"])
        if cur_row is None:
            continue

        available = r["available_qty"] or 0
        qty_to_push = available
        if r["low_stock_qty"] is not None:
            qty_to_push = max(available - r["low_stock_qty"], 0)

        price_changed = (
            cur_row["pushed_price"] is None
            or abs(float(cur_row["pushed_price"]) - float(r["resolved_price"])) >= 0.005
        )
        qty_changed = cur_row["pushed_qty"] is None or cur_row["pushed_qty"] != qty_to_push

        if not (price_changed or qty_changed):
            continue

        change = {
            "row_id": r["row_id"],
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
                "platform/account sync disabled" if not (kill_switch_ok.get(cur_row["account"], True) and kill_switch_ok.get(None, True))
                else "sync_enabled=false" if not cur_row["sync_enabled"]
                else f"status={cur_row['status']!r}"
            )
            skipped_ungated.append({**change, "reason": reason})

    return resolved, changes, skipped_ungated


def push_prices(listing_id: str, account_num: int = 1, platform: str = "ebay",
                 dry_run: bool = False, quiet: bool = False) -> dict:
    """
    Returns a summary dict (used by the /push-prices API endpoint, same
    convention as importer.ebay_picking.pull_picking):
      {"listing_id", "resolved", "changed", "pushed", "warnings": [...],
       "dry_run": bool}
    """
    account = get_account_name(account_num)

    def p(msg):
        if not quiet:
            print(msg)

    with db_cursor() as cur:
        resolved, changes, skipped_ungated = _compute_changes(cur, platform, listing_id)
        summary = {"listing_id": listing_id, "resolved": len(resolved), "changed": len(changes),
                   "pushed": 0, "warnings": [], "dry_run": dry_run}

        if not resolved:
            p(f"No platform_listings rows found for {platform} listing {listing_id}.")
            return summary

        if skipped_ungated:
            for s in skipped_ungated:
                msg = f"{s['derived_label']} ({s['external_id']}): would change but not synced — {s['reason']}"
                summary["warnings"].append(msg)
                p(f"  [NOT SYNCED] {msg}")

        if not changes:
            p(f"[{listing_id}] nothing gated-in with pending changes "
              f"({len(resolved)} row(s) resolved, {len(skipped_ungated)} skipped as not synced) — nothing to push.")
            return summary

        item = fetch_item(listing_id, account_num=account_num)
        variations_node = _find(item, "Variations")

        if variations_node is None:
            # Single-listing path: one line, no <Variations> block at all.
            change = changes[0]
            qty_sold = get_quantity_sold(item)
            qty_to_set = qty_sold + change["qty_to_push"]

            p(f"  [single] {change['derived_label']}: ${change['resolved_price']:.2f}, "
              f"qty -> {qty_to_set} (sold={qty_sold} + push={change['qty_to_push']}) "
              f"[{change['price_source']}]")

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
                (change["resolved_price"], change["qty_to_push"], change["row_id"]),
            )
            summary["pushed"] = 1
            p(f"[{listing_id}] pushed 1 change.")
            return summary

        # Multi-variation path
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

        if not pushed:
            p(f"[{listing_id}] {len(changes)} change(s) computed, but none matched a live variation — nothing pushed.")
            return summary

        if dry_run:
            p(f"[DRY-RUN] would push {len(pushed)} of {len(resolved)} row(s) to {listing_id} "
              f"(only the changed ones — not re-sending all {len(resolved)})")
            summary["pushed"] = len(pushed)  # "would push" count, nothing actually sent
            return summary

        xml = build_revise_xml(listing_id, variations, "ReviseFixedPriceItem", account_num=account_num)
        _post("ReviseFixedPriceItem", xml, account_num=account_num)

        for change in pushed:
            cur.execute(
                "UPDATE platform_listings SET pushed_price = %s, pushed_qty = %s, pushed_at = now() WHERE id = %s",
                (change["resolved_price"], change["qty_to_push"], change["row_id"]),
            )

        summary["pushed"] = len(pushed)
        p(f"[{listing_id}] pushed {len(pushed)} of {len(resolved)} row(s).")
        return summary
