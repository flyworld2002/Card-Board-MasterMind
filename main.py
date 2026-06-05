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
        print("Error: --variant required. e.g. --variant 'Cosmos Holo'")
        return

    SPECIAL_PATTERNS = {"Cosmos Holo", "Master Ball Pattern", "Poke Ball Pattern",
                        "Cracked Ice Holo", "Galaxy Holo"}

    foil_pattern = variant if variant in SPECIAL_PATTERNS else None
    foil_type    = "Holo" if foil_pattern else variant
    is_special   = variant in SPECIAL_PATTERNS

    with db_cursor() as cur:
        query = """
            UPDATE card_variants cv
               SET variant_type = %s,
                   finish        = %s,
                   is_special    = %s
              FROM card_master cm
             WHERE cv.card_id = cm.id
               AND cm.name = %s
        """
        params = [variant, foil_type, is_special, name]
        if number:
            query  += " AND cm.card_number = %s"
            params.append(number)
        cur.execute(query, params)
        count = cur.rowcount
        print(f"Updated {count} variant row(s) for {name}" +
              (f" #{number}" if number else "") +
              f" → {variant}")

def cmd_stock(args):
    from db.connection import get_stock_summary
    rows = get_stock_summary()
    if not rows:
        print("No inventory found.")
        return

    header = "\n" + f"{'Card':<30} {'Set':<22} {'Number':<10} {'Variant':<22} {'Cond':<18} {'Qty':>4} {'Cost':>7} {'List':>7} {'Market':>8}"
    print(header)
    print("-" * 132)

    for r in rows:
        variant_str = str(r.get('variant_type') or '-')
        market      = r.get('market_price')
        num         = str(r.get('display_number') or r.get('card_number') or '-')
        print(
            f"{str(r['card_name']):<30} "
            f"{str(r['set_name']):<22} "
            f"{num:<10} "
            f"{variant_str:<22} "
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
    ok = verify_credentials()
    if not ok:
        print("\nFix your .env credentials and try again.")
        sys.exit(1)

def cmd_ebay_import(args):
    """Fetch all active eBay listings → staging table."""
    from importer.ebay import import_from_ebay
    import_from_ebay(dry_run=args.dry_run)

def cmd_ebay_item(args):
    from importer.ebay import import_single_item
    import_single_item(args.ebay_item, dry_run=args.dry_run, no_api=getattr(args, 'no_api', False))

def cmd_ebay_export(args):
    from importer.ebay import export_listings_to_csv
    export_listings_to_csv(
        no_api=getattr(args, 'no_api', False),
        item_id=getattr(args, 'export_item', None)
    )

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

    # ── Shared optional flags ─────────────────────────────────────────────────
    parser.add_argument("--dry-run", action="store_true",
        help="Parse only, no DB writes")
    parser.add_argument("--no-api", action="store_true",
        help="Skip API calls during dry run — just show parsed eBay data")
    parser.add_argument("--order", metavar="ORDER_NUM",
        help="Only process this specific order number")
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
if __name__ == "__main__":
    main()
