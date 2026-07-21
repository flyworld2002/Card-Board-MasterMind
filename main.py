#!/usr/bin/env python3

"""
main.py — Card Inventory Tool

Import:
  python3 main.py --tcgplayer-html order.html   # import HTML to staging
  python3 main.py --tcgplayer-html ~/orders/ --dry-run  # dry run a folder
  python3 main.py --manual                      # manual purchase entry
  python3 main.py --ebay-import                 # import active eBay listings → staging
  python3 main.py --ebay-import --dry-run       # preview without writing to DB
  python3 main.py --ebay-verify                 # verify eBay credentials only

Staging workflow:
  python3 main.py --review        # review staged items, fix conditions/qty/price
  python3 main.py --approve       # push approved items to inventory
  python3 main.py --approve-all   # push ALL pending items to inventory

Images:
  python3 main.py --upload-image photo.jpg --search "Charizard"

Reports:
  python3 main.py --stock         # current inventory summary
"""

import argparse
import sys

def cmd_tcgplayer_html(args):
    from importer.tcgplayer_html import import_from_html
    import_from_html(args.tcgplayer_html, dry_run=args.dry_run,
                     only_order=getattr(args, "order", None))

def cmd_manual(args):
    from importer.manual import manual_import
    manual_import()

def cmd_review(args):
    from importer.staging_workflow import review_staging
    review_staging()

def cmd_approve(args):
    from importer.staging_workflow import approve_staging
    approve_staging()

def cmd_approve_order(args):
    from importer.staging_workflow import approve_order
    approve_order(args.approve_order)

def cmd_approve_all(args):
    from importer.staging_workflow import approve_all_staging
    approve_all_staging()

def cmd_upload_image(args):
    from importer.image_upload import upload_own_image
    upload_own_image(
        image_path  = args.upload_image,
        card_id     = getattr(args, "card_id", None),
        search_name = getattr(args, "search", None),
    )

def cmd_fix_variant(args):
    from db.connection import db_cursor

    name    = args.fix_variant
    number  = getattr(args, "number", None)
    variant = getattr(args, "variant", None)

    if not variant:
        print("Error: --variant required. e.g. --variant 'reverse_holo' or 'poke_ball'")
        return

    # Map a single --variant token onto the right seven-axis column.
    FOIL_TYPES    = {"non_holo", "holo", "reverse_holo"}
    FOIL_PATTERNS = {"poke_ball", "master_ball", "friend_ball", "love_ball",
                     "quick_ball", "dusk_ball", "team_rocket", "energy_symbol"}
    TEXTURES      = {"cosmos", "hd_cosmos", "galaxy_cosmos"}
    MATERIALS     = {"metal"}
    SIZES         = {"jumbo"}
    STAMPS        = {"1st_edition", "pokemon_center", "prerelease",
                     "pokemon_day", "mega_evolution", "prismatic_evolution"}
    SOURCES       = {"deck_exclusive", "product_exclusive", "box_topper", "stamp_promo"}

    v = variant.strip().lower()
    col = None
    for colname, valset in (("foil_type", FOIL_TYPES), ("foil_pattern", FOIL_PATTERNS),
                            ("texture", TEXTURES), ("material", MATERIALS),
                            ("size", SIZES), ("stamp_type", STAMPS),
                            ("source_type", SOURCES)):
        if v in valset:
            col = colname
            break

    if not col:
        print(f"Error: '{variant}' is not a recognized axis value.")
        print("  foil_type:    non_holo, holo, reverse_holo")
        print("  foil_pattern: poke_ball, master_ball, friend_ball, love_ball, "
              "quick_ball, dusk_ball, team_rocket, energy_symbol")
        print("  texture:      cosmos, hd_cosmos, galaxy_cosmos")
        print("  material:     metal    |  size: jumbo")
        print("  stamp_type:   1st_edition, pokemon_center, prerelease, "
              "pokemon_day, mega_evolution, prismatic_evolution")
        print("  source_type:  deck_exclusive, product_exclusive, box_topper, stamp_promo")
        return

    with db_cursor() as cur:
        query = f"""
            UPDATE card_variants cv
               SET {col} = %s
              FROM card_master cm
             WHERE cv.card_id = cm.id
               AND cm.name = %s
        """
        params = [v, name]
        if number:
            query  += " AND cm.card_number = %s"
            params.append(number)
        cur.execute(query, params)
        count = cur.rowcount
        print(f"Updated {count} variant row(s) for {name}" +
              (f" #{number}" if number else "") +
              f" → {col}={v}")

def _variant_label(r: dict) -> str:
    """Build a display label from the seven axes (skip blanks)."""
    DISPLAY = {
        "non_holo": "Non-Holo", "holo": "Holo", "reverse_holo": "Reverse Holo",
        "poke_ball": "Poke Ball", "master_ball": "Master Ball",
        "friend_ball": "Friend Ball", "love_ball": "Love Ball",
        "quick_ball": "Quick Ball", "dusk_ball": "Dusk Ball",
        "team_rocket": "Team Rocket", "energy_symbol": "Energy Symbol",
        "cosmos": "Cosmos", "hd_cosmos": "HD Cosmos", "galaxy_cosmos": "Galaxy Cosmos",
        "metal": "Metal", "jumbo": "Jumbo",
        "1st_edition": "1st Edition", "pokemon_center": "Pokemon Center",
        "prerelease": "Prerelease", "pokemon_day": "Pokemon Day",
        "mega_evolution": "Mega Evolution", "prismatic_evolution": "Prismatic Evolution",
        "deck_exclusive": "Deck Exclusive", "product_exclusive": "Product Exclusive",
        "box_topper": "Box Topper", "stamp_promo": "Stamp Promo",
    }
    parts = []
    for key in ("foil_type", "foil_pattern", "texture", "material", "size",
                "variant_stamp_type", "variant_source_type"):
        val = r.get(key)
        if val:
            parts.append(DISPLAY.get(val, val))
    return " · ".join(parts) if parts else "-"


def cmd_stock(args):
    from db.connection import get_stock_summary
    rows = get_stock_summary()
    if not rows:
        print("No inventory found.")
        return

    header = "\n" + f"{'Card':<30} {'Set':<22} {'Number':<10} {'Variant':<30} {'Cond':<18} {'Qty':>4} {'Cost':>7} {'List':>7} {'Market':>8}"
    print(header)
    print("-" * 140)

    for r in rows:
        variant_str = _variant_label(r)
        market      = r.get('market_price')
        num         = str(r.get('display_number') or r.get('card_number') or '-')
        print(
            f"{str(r['card_name']):<30} "
            f"{str(r['set_name']):<22} "
            f"{num:<10} "
            f"{variant_str:<30} "
            f"{str(r['condition']):<18} "
            f"{r['qty_available']:>4} "
            f"{'$' + '{:.2f}'.format(r['avg_cost_basis'] or 0):>7} "
            f"{'$' + '{:.2f}'.format(r['asking_price']) if r['asking_price'] else '-':>7} "
            f"{'$' + '{:.2f}'.format(market) if market else '-':>8}"
        )
    print("\nTotal: " + str(len(rows)) + " row(s)")

# ── eBay commands ─────────────────────────────────────────────────────────────

def cmd_ebay_verify(args):
    """Quick credential check — no DB writes."""
    from importer.ebay_auth import verify_credentials
    ok = verify_credentials(account_num=args.account)
    if not ok:
        print("\nFix your .env credentials and try again.")
        sys.exit(1)

def cmd_ebay_import(args):
    """Fetch all active eBay listings → staging table."""
    from importer.ebay import import_from_ebay
    import_from_ebay(dry_run=args.dry_run, account_num=args.account)

def cmd_ebay_item(args):
    from importer.ebay import import_single_item
    import_single_item(args.ebay_item, dry_run=args.dry_run, no_api=getattr(args, 'no_api', False),
                       account_num=args.account)

def cmd_ebay_export(args):
    from importer.ebay import export_listings_to_csv
    export_listings_to_csv(
        no_api=getattr(args, 'no_api', False),
        item_id=getattr(args, 'export_item', None)
    )

def cmd_ebay_pullorders(args):
    from importer.ebay_orders import pull_orders
    needs_attention = pull_orders(
        account_num=args.account,
        since_str=getattr(args, 'since', None),
        until_str=getattr(args, 'until', None),
        order_ids=getattr(args, 'order_id', None),
        dry_run=args.dry_run,
        paid_since_str=getattr(args, 'paid_since', None),
        quiet=getattr(args, 'quiet', False),
    )
    if needs_attention:
        sys.exit(1)

def cmd_ebay_backfill_orderids(args):
    from importer.ebay_orders import backfill_order_ids
    backfill_order_ids(
        account_num=args.account,
        since_str=getattr(args, 'since', None),
        until_str=getattr(args, 'until', None),
        dry_run=args.dry_run,
    )

def cmd_ebay_set_label_cost(args):
    from importer.ebay_syncfees import set_label_cost
    set_label_cost(
        order_id=args.order_id[0] if args.order_id else None,
        amount=args.amount,
        account_num=args.account,
        is_return_label=getattr(args, 'return_label', False),
        dry_run=args.dry_run,
    )

def cmd_ebay_syncfees(args):
    from importer.ebay_syncfees import sync_fees
    since_str = getattr(args, 'since', None)
    since_days = getattr(args, 'since_days', None)
    if not since_str and since_days:
        from datetime import datetime, timedelta, timezone
        since_str = (datetime.now(timezone.utc) - timedelta(days=since_days)).strftime('%Y-%m-%dT%H:%M:%S')
    sync_fees(
        account_num=args.account,
        since_str=since_str,
        until_str=getattr(args, 'until', None),
        order_ids=getattr(args, 'order_id', None),
        dry_run=args.dry_run,
    )

def cmd_ebay_pullpicking(args):
    from importer.ebay_picking import pull_picking
    import sys as _sys
    # --account only narrows the pull when explicitly passed; the default
    # behavior is ALL accounts with a refresh token in .env.
    account_nums = [args.account] if "--account" in _sys.argv else None
    pull_picking(
        account_nums=account_nums,
        dry_run=args.dry_run,
        quiet=getattr(args, 'quiet', False),
    )

def cmd_ebay_fulfillment_test(args):
    from importer.ebay_finances import test_fetch_order
    test_fetch_order(order_id=args.fin_order, account_num=args.account)

def cmd_ebay_finances_test(args):
    from importer.ebay_finances import test_fetch_transactions
    test_fetch_transactions(order_id=args.fin_order, account_num=args.account)

def cmd_ebay_reconcile(args):
    from importer.ebay_reconcile import reconcile_listings
    reconcile_listings(account_num=args.account, fix=getattr(args, 'fix', False))

def cmd_ebay_recalc_prices(args):
    from importer.ebay_listing_sync import recalc
    recalc(account_num=args.account, item_id=getattr(args, 'item_id', None),
           card_query=getattr(args, 'card', None), dry_run=args.dry_run,
           quiet=args.quiet, allow_decreases=getattr(args, 'allow_decreases', False))

def cmd_ebay_push_listings(args):
    from importer.ebay_listing_sync import push
    push(account_num=args.account, item_id=getattr(args, 'item_id', None),
         card_query=getattr(args, 'card', None), dry_run=args.dry_run,
         quiet=args.quiet, force=getattr(args, 'force', False))

# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Card inventory tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )

    group = parser.add_mutually_exclusive_group(required=True)

    group.add_argument("--tcgplayer-html", metavar="PATH",
        help="Import TCGPlayer saved HTML file or folder → staging")
    group.add_argument("--manual", action="store_true",
        help="Manually enter a purchase")
    group.add_argument("--review", action="store_true",
        help="Review and fix staged items interactively")
    group.add_argument("--approve", action="store_true",
        help="Push approved staging items to inventory")
    group.add_argument("--approve-all", action="store_true",
        help="Approve and push ALL pending staging items to inventory")
    group.add_argument("--approve-order", metavar="ORDER_NUM",
        help="Approve and push a specific order to inventory")
    group.add_argument("--upload-image", metavar="FILE",
        help="Upload your own card photo to Cloudflare R2")
    group.add_argument("--stock", action="store_true",
        help="Show current inventory summary")
    group.add_argument("--fix-variant", metavar="CARD_NAME",
        help="Fix variant for a card. Use with --number and --variant")

    # ── New eBay flags ────────────────────────────────────────────────────────
    group.add_argument("--ebay-import", action="store_true",
        help="Import active eBay listings → staging (use --dry-run to preview)")
    group.add_argument("--ebay-verify", action="store_true",
        help="Verify eBay API credentials without importing anything")
    group.add_argument("--ebay-item", metavar="ITEM_ID",
        help="Import a single eBay listing by item ID → staging")
    group.add_argument("--ebay-export", action="store_true",
    help=(
        "Export all active eBay listings to CSV for review. "
        "No DB writes. "
        "Default: calls Pokemon TCG API to match cards. "
        "Use --no-api for instant export without API calls."
    ))
    group.add_argument("--ebay-pullorders", action="store_true",
        help="Pull eBay orders and record sales (use --dry-run to preview)")
    group.add_argument("--ebay-backfill-orderids", action="store_true",
        help="One-time: re-fetch orders and set real eBay OrderID on historical "
             "sales rows, matching on order_line_item_id "
             "(use with --since/--until/--account/--dry-run)")
    group.add_argument("--ebay-reconcile", action="store_true",
        help="Diff platform_listings against eBay's live quantities (use --fix to apply eBay's numbers)")
    group.add_argument("--ebay-set-label-cost", action="store_true",
        help="Manually set label_cost (or --return-label for return_label_cost) "
             "on one order's sale_orders row, for cases where eBay's Finances "
             "API never posts a SHIPPING_LABEL transaction. Requires --order-id "
             "and --amount; the order must already have been synced once via "
             "--ebay-syncfees so the header row exists.")
    group.add_argument("--ebay-syncfees", action="store_true",
        help="Sync real fee/discount/refund/buyer data from eBay's Finances + "
             "Fulfillment APIs into sale_orders / sale_line_item_fees. Targets "
             "orders from --since/--until, or specific --order-id(s). Use "
             "--dry-run to preview without writing.")
    group.add_argument("--ebay-pullpicking", action="store_true",
        help="Snapshot paid-but-unshipped orders into picking_queue for the "
             "Picking tab (all accounts by default; --account N to narrow)")
    group.add_argument("--ebay-finances-test", action="store_true",
        help="One-time connectivity test for the Finances API OAuth setup — "
             "fetches real transactions for --fin-order and prints the raw "
             "response. No DB writes.")
    group.add_argument("--ebay-fulfillment-test", action="store_true",
        help="Connectivity test for the Fulfillment API (getOrder) — fetches "
             "pricingSummary (discount, shipping charged) for --fin-order. "
             "Requires a refresh token consented with sell.fulfillment scope.")
    group.add_argument("--ebay-recalc-prices", action="store_true",
        help="Recalculate list_price for sync-enabled platform_listings rows "
             "(DB-only, never calls eBay). Scope with --item-id or --card; "
             "use --dry-run to preview. See docs/plans/ebay-listing-sync.md.")
    group.add_argument("--ebay-push-listings", action="store_true",
        help="Push recalculated price/quantity to eBay via ReviseFixedPriceItem "
             "(NOT YET IMPLEMENTED — see importer/ebay_listing_sync.py). "
             "Scope with --item-id or --card; --force to push unchanged listings.")

    # ── Shared optional flags ─────────────────────────────────────────────────
    parser.add_argument("--dry-run", action="store_true",
        help="Parse only, no DB writes")
    parser.add_argument("--quiet", action="store_true",
        help="Minimal output — one summary line, full detail only if issues need attention "
             "(for --ebay-pullorders; intended for scheduled/unattended runs)")
    parser.add_argument("--no-api", action="store_true",
        help="Skip API calls during dry run — just show parsed eBay data")
    parser.add_argument("--order", metavar="ORDER_NUM",
        help="Only process this specific order number")
    parser.add_argument("--since", metavar="DATE",
        help="Start of pull window, e.g. 2026-07-01 (for --ebay-pullorders)")
    parser.add_argument("--until", metavar="DATE",
        help="End of pull window, e.g. 2026-07-03 (for --ebay-pullorders)")
    parser.add_argument("--order-id", metavar="ORDER_ID", action="append", default=None,
        help="Targeted eBay order ID to pull (repeatable, for --ebay-pullorders)")
    parser.add_argument("--fix", action="store_true",
        help="Apply eBay's numbers as truth (for --ebay-reconcile)")
    parser.add_argument("--fin-order", metavar="ORDER_ID",
        help="Real eBay order ID to test the Finances API against (for --ebay-finances-test)")
    parser.add_argument("--since-days", metavar="N", type=int,
        help="Alternative to --since: sync/pull orders from the last N days "
             "(rolling window, computed at run time — useful for a daily "
             "scheduled job so it doesn't need external date math). For --ebay-syncfees.")
    parser.add_argument("--amount", metavar="DOLLARS", type=float,
        help="Dollar amount to set (for --ebay-set-label-cost)")
    parser.add_argument("--return-label", action="store_true",
        help="Target return_label_cost instead of label_cost (for --ebay-set-label-cost)")
    parser.add_argument("--paid-since", metavar="DATE",
        help="Only record sales paid on/after this date, e.g. 2026-07-03T00:00:00 "
             "(for --ebay-pullorders). Persists across future runs once set; "
             "protects existing inventory from sales that predate your import.")
    parser.add_argument("--export-item", metavar="ITEM_ID",
        help="Export a single eBay listing by item ID (use with --ebay-export)")
    parser.add_argument("--number", metavar="CARD_NUM",
        help="Card number for --fix-variant")
    parser.add_argument("--variant", metavar="VARIANT",
        help="Correct variant for --fix-variant")
    parser.add_argument("--card-id", metavar="UUID",
        help="Card UUID (for --upload-image)")
    parser.add_argument("--search", metavar="NAME",
        help="Card name search (for --upload-image)")
    parser.add_argument("--account", metavar="N", type=int, default=1,
        help="eBay account number to use, e.g. --account 2 (matches EBAY_ACCOUNT_{N}_* in .env). Defaults to 1.")
    parser.add_argument("--item-id", metavar="ITEM_ID",
        help="Scope to one eBay listing (for --ebay-recalc-prices / --ebay-push-listings). "
             "Must already have sync_enabled=true — a disabled listing is skipped even "
             "when explicitly targeted.")
    parser.add_argument("--card", metavar="NAME",
        help="Scope to one card by name, across whichever sync-enabled listings hold it "
             "(for --ebay-recalc-prices / --ebay-push-listings).")
    parser.add_argument("--allow-decreases", action="store_true",
        help="Bypass the never-lower price guard (for --ebay-recalc-prices).")
    parser.add_argument("--force", action="store_true",
        help="Push all listings in scope regardless of pending-changes detection "
             "(for --ebay-push-listings).")

    args = parser.parse_args()

    if args.tcgplayer_html:
        cmd_tcgplayer_html(args)
    elif args.manual:
        cmd_manual(args)
    elif args.review:
        cmd_review(args)
    elif args.approve:
        cmd_approve(args)
    elif args.approve_all:
        cmd_approve_all(args)
    elif args.approve_order:
        cmd_approve_order(args)
    elif args.upload_image:
        cmd_upload_image(args)
    elif args.stock:
        cmd_stock(args)
    elif args.fix_variant:
        cmd_fix_variant(args)
    elif args.ebay_import:
        cmd_ebay_import(args)
    elif args.ebay_verify:
        cmd_ebay_verify(args)
    elif args.ebay_item:
        cmd_ebay_item(args)    
    elif args.ebay_export:
        cmd_ebay_export(args)
    elif args.ebay_pullorders:
        cmd_ebay_pullorders(args)
    elif args.ebay_backfill_orderids:
        cmd_ebay_backfill_orderids(args)
    elif args.ebay_reconcile:
        cmd_ebay_reconcile(args)
    elif args.ebay_set_label_cost:
        cmd_ebay_set_label_cost(args)
    elif args.ebay_syncfees:
        cmd_ebay_syncfees(args)
    elif args.ebay_pullpicking:
        cmd_ebay_pullpicking(args)
    elif args.ebay_finances_test:
        cmd_ebay_finances_test(args)
    elif args.ebay_fulfillment_test:
        cmd_ebay_fulfillment_test(args)
    elif args.ebay_recalc_prices:
        cmd_ebay_recalc_prices(args)
    elif args.ebay_push_listings:
        cmd_ebay_push_listings(args)
if __name__ == "__main__":
    main()
