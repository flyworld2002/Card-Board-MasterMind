"""
importer/staging_workflow.py
Interactive review and approve workflow for staged imports.

--review: Shows staged cards, lets you fix conditions, quantities,
          remove refunded cards, resolve ambiguous matches, set manual prices.

--approve: Pushes all approved staging rows to real inventory.
"""

from datetime import datetime, timezone
from db.staging import (
    get_pending_batches, get_staging_rows,
    update_staging_row, approve_batch
)
from db.connection import insert_inventory, insert_purchase

CONDITIONS = ["Near Mint", "Lightly Played", "Moderately Played",
              "Heavily Played", "Damaged"]


# ----------------------------------------------------------------
# --review command
# ----------------------------------------------------------------

def review_staging():
    """Interactive review of staged imports."""
    batches = get_pending_batches()
    if not batches:
        print("\nNo pending imports in staging.")
        print("Run  python3 main.py --tcgplayer-html <file>  to import first.\n")
        return

    print("\n=== Staging Review ===\n")

    # Pick a batch
    if len(batches) == 1:
        batch = batches[0]
    else:
        print("Pending batches:")
        for i, b in enumerate(batches, 1):
            print(f"  {i}. {b['import_batch']} — "
                  f"{b['total_rows']} cards "
                  f"({b['pending']} pending, {b['approved']} approved, {b['skipped']} skipped) "
                  f"imported {_fmt_dt(b['imported_at'])}")
        choice = input("\nPick batch number: ").strip()
        if not choice.isdigit() or not (1 <= int(choice) <= len(batches)):
            print("Invalid choice.")
            return
        batch = batches[int(choice) - 1]

    batch_id = batch["import_batch"]
    rows     = get_staging_rows(batch_id=batch_id, status="pending")

    if not rows:
        print(f"\nNo pending rows in batch {batch_id}.")
        print("All items already reviewed. Run  python3 main.py --approve  to push to inventory.")
        return

    print(f"\nBatch: {batch_id}")
    print(f"Pending: {len(rows)} card(s)\n")
    print("Commands: (a)pprove  (s)kip  (c)ondition  (q)uantity  (p)rice  (n)otes  (r)esolve  (d)one\n")

    i = 0
    while i < len(rows):
        row = rows[i]
        _print_row(row, i + 1, len(rows))

        cmd = input("  > ").strip().lower()

        if cmd in ("a", "approve", ""):
            update_staging_row(str(row["id"]), status="approved")
            print("  ✓ Approved\n")
            i += 1

        elif cmd in ("s", "skip"):
            reason = input("  Reason (optional): ").strip()
            update_staging_row(str(row["id"]), status="skipped",
                               notes=reason or "Skipped during review")
            print("  ✗ Skipped\n")
            i += 1

        elif cmd in ("c", "condition"):
            print("  Conditions:")
            for j, c in enumerate(CONDITIONS, 1):
                print(f"    {j}. {c}")
            pick = input("  Pick number: ").strip()
            if pick.isdigit() and 1 <= int(pick) <= len(CONDITIONS):
                new_cond = CONDITIONS[int(pick) - 1]
                update_staging_row(str(row["id"]), condition=new_cond)
                row = dict(row)
                row["condition"] = new_cond
                print(f"  Updated condition → {new_cond}\n")
            else:
                print("  Invalid. No change.\n")

        elif cmd in ("q", "quantity"):
            qty = input("  New quantity: ").strip()
            if qty.isdigit() and int(qty) >= 0:
                update_staging_row(str(row["id"]), quantity=int(qty))
                row = dict(row)
                row["quantity"] = int(qty)
                print(f"  Updated quantity → {qty}\n")
            else:
                print("  Invalid. No change.\n")

        elif cmd in ("p", "price"):
            print(f"  Current cost basis: ${row['price']:.2f}")
            if row.get("calculated_price"):
                print(f"  Suggested list price: ${row['calculated_price']:.2f}")
            price = input("  Override list price (or blank to skip): $").strip()
            if price:
                try:
                    update_staging_row(str(row["id"]), override_price=float(price))
                    print(f"  Override price set → ${float(price):.2f}\n")
                except ValueError:
                    print("  Invalid price. No change.\n")

        elif cmd in ("n", "notes"):
            note = input("  Note: ").strip()
            update_staging_row(str(row["id"]), notes=note)
            print("  Note saved.\n")

        elif cmd in ("r", "resolve"):
            _resolve_ambiguous(row)

        elif cmd in ("d", "done"):
            print("\nReview paused. Run again to continue.\n")
            break

        elif cmd == "back":
            i = max(0, i - 1)

        else:
            print("  Unknown command. Try: a / s / c / q / p / n / r / d\n")

    # Summary
    remaining = get_staging_rows(batch_id=batch_id, status="pending")
    approved  = get_staging_rows(batch_id=batch_id, status="approved")
    print(f"\nBatch summary — Approved: {len(approved)} | "
          f"Still pending: {len(remaining)}")

    if remaining:
        print("Run  python3 main.py --review  to continue reviewing.")
    else:
        print("All items reviewed!")
        if approved:
            print("Run  python3 main.py --approve  to push to inventory.")


def _print_row(row: dict, current: int, total: int):
    """Print a staging row for review."""
    match_icon = {"matched": "✓", "ambiguous": "?", "not_found": "✗"}.get(
        row.get("match_status", ""), "·"
    )
    print(f"  [{current}/{total}] {match_icon} {row['card_name']}")
    print(f"        Set:       {row.get('set_name') or row.get('matched_set_name') or '—'}")
    print(f"        Condition: {row['condition']}")
    print(f"        Qty:       {row['quantity']}")
    print(f"        Cost:      ${row['price']:.2f}")
    if row.get("calculated_price"):
        override = row.get("override_price")
        price_str = f"${override:.2f} (override)" if override else f"${row['calculated_price']:.2f} (suggested)"
        print(f"        List $:    {price_str}")
    if row.get("match_status") == "ambiguous":
        print(f"        ⚠ Ambiguous match — use (r) to resolve")
    if row.get("match_status") == "not_found":
        print(f"        ⚠ Not found in API — use (r) to search manually")
    if row.get("notes"):
        print(f"        Notes:     {row['notes']}")
    print()


def _resolve_ambiguous(row: dict):
    """Interactive card resolution for ambiguous or not-found cards."""
    import json
    from db.connection import find_card_by_external_id
    from utils.pokemon_api import search_cards, parse_card_master_fields, parse_card_attribute_fields
    from db.connection import get_game_id, get_or_create_set, insert_card_master, insert_card_attributes

    print(f"\n  Resolving: {row['card_name']}")

    # Use stored options if available
    options = []
    if row.get("match_options"):
        raw = row["match_options"]
        options = json.loads(raw) if isinstance(raw, str) else raw

    if not options:
        search_name = input(f"  Search name [{row['card_name']}]: ").strip() or row["card_name"]
        set_name    = input(f"  Set name [{row.get('set_name','')}]: ").strip() or row.get("set_name", "")
        results     = search_cards(name=search_name, set_name=set_name)
        options     = [{"id": c["id"], "name": c["name"],
                        "card_number": c.get("number"),
                        "set": c["set"]["name"],
                        "variant": ", ".join(c.get("subtypes", []))}
                       for c in results]

    if not options:
        print("  No results found.")
        skip = input("  Skip this card? (y/n) [y]: ").strip().lower()
        if skip != "n":
            update_staging_row(str(row["id"]), status="skipped",
                               notes="Not found in API")
        return

    print(f"\n  Found {len(options)} option(s):")
    for i, o in enumerate(options, 1):
        print(f"    {i}. {o['name']} #{o.get('card_number','?')} | "
              f"{o.get('set','?')} | {o.get('variant','—')}")

    choice = input(f"\n  Pick number (1-{len(options)}) or 's' to skip: ").strip()
    if choice.lower() == "s" or not choice.isdigit():
        update_staging_row(str(row["id"]), status="skipped", notes="Could not resolve")
        print("  Skipped.\n")
        return

    idx     = int(choice) - 1
    api_id  = options[idx]["id"]

    # Get or create card_master
    existing = find_card_by_external_id(api_id)
    if existing:
        card_id = str(existing["id"])
    else:
        from utils.pokemon_api import get_card_by_id
        api_card = get_card_by_id(api_id)
        if not api_card:
            print("  Could not fetch card from API.")
            return
        game_id     = get_game_id("Pokemon")
        fields      = parse_card_master_fields(api_card)
        attr_fields = parse_card_attribute_fields(api_card)
        set_id = get_or_create_set(
            game_id=game_id, name=fields["set_name"], set_code=fields["set_code"],
            series=fields.get("series"), release_year=fields.get("release_year"),
            total_cards=fields.get("total_cards"),
        )
        card_id = insert_card_master(
            set_id=set_id, name=fields["name"], card_number=fields["card_number"],
            rarity=fields.get("rarity"), variant=fields.get("variant"),
            finish=fields.get("finish"), is_promo=fields.get("is_promo", False),
            is_first_edition=fields.get("is_first_edition", False),
            image_url=fields.get("image_url"), external_id=fields["external_id"],
        )
        insert_card_attributes(card_id, **attr_fields)

    update_staging_row(str(row["id"]), card_id=card_id, match_status="matched")
    print(f"  ✓ Resolved to: {options[idx]['name']} #{options[idx].get('card_number','?')}\n")


# ----------------------------------------------------------------
# --approve command
# ----------------------------------------------------------------

def approve_staging():
    """Push all approved staging rows to real inventory."""
    batches = get_pending_batches()

    # Also check for batches with approved but not-yet-processed rows
    from db.connection import db_cursor
    with db_cursor() as cur:
        cur.execute("""
            SELECT DISTINCT import_batch FROM staging
            WHERE status = 'approved'
            AND import_batch NOT IN (
                SELECT DISTINCT import_batch FROM staging
                WHERE status = 'processed'
            )
            ORDER BY import_batch DESC
        """)
        approved_batches = [r["import_batch"] for r in cur.fetchall()]

    if not approved_batches:
        print("\nNo approved items to push to inventory.")
        print("Run  python3 main.py --review  first.\n")
        return

    print("\n=== Approve Staging → Inventory ===\n")

    if len(approved_batches) == 1:
        batch_id = approved_batches[0]
    else:
        print("Batches with approved items:")
        for i, b in enumerate(approved_batches, 1):
            print(f"  {i}. {b}")
        choice = input("\nPick batch number (or 'all'): ").strip()
        if choice.lower() == "all":
            for b in approved_batches:
                _push_batch(b)
            return
        if not choice.isdigit() or not (1 <= int(choice) <= len(approved_batches)):
            print("Invalid choice.")
            return
        batch_id = approved_batches[int(choice) - 1]

    _push_batch(batch_id)


def _push_batch(batch_id: str):
    """Push one batch of approved staging rows to inventory.
    Uses a single DB connection for the entire batch — much faster.
    """
    rows = approve_batch(batch_id)
    if not rows:
        print(f"No approved rows in {batch_id}.")
        return

    print(f"\nPushing {len(rows)} card(s) from {batch_id} to inventory...")

    # Group by order number to create one purchase per order
    orders: dict[str, list] = {}
    for row in rows:
        key = row["order_number"] or batch_id
        orders.setdefault(key, []).append(row)

    SPECIAL_PATTERNS = {
        "Cosmos Holo", "Master Ball Pattern", "Poke Ball Pattern",
        "Cracked Ice Holo", "Galaxy Holo"
    }

    pushed = 0

    # ── Single connection for the entire batch ────────────────────────────────
    from db.connection import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()

        for order_num, order_rows in orders.items():
            order_date = order_rows[0]["order_date"] or datetime.now(timezone.utc)
            is_ebay    = order_rows[0].get("source") == "ebay"

            # ── Create purchase record ────────────────────────────────────────
            total_cost = sum(float(r["price"]) * int(r["quantity"]) for r in order_rows)
            cur.execute("""
                INSERT INTO purchases
                    (source, purchase_type, reference_id, total_cost,
                     card_count, notes, purchased_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (
                order_rows[0].get("source", "tcgplayer"),
                "lot" if len(order_rows) > 1 else "single",
                order_num,
                total_cost,
                sum(int(r["quantity"]) for r in order_rows),
                None,
                order_date,
            ))
            purchase_id = str(cur.fetchone()["id"])

            # ── Insert each card ──────────────────────────────────────────────
            for row in order_rows:
                list_price   = row.get("override_price") or row.get("calculated_price")
                foil_type    = row.get("foil_type")
                foil_pattern = row.get("foil_pattern")
                variant_type = foil_pattern or foil_type or "Non-Holo"
                finish       = foil_type or "Non-Holo"
                is_special   = variant_type in SPECIAL_PATTERNS

                # Cost vs asking price depends on source
                if is_ebay:
                    cost   = 0.0
                    asking = float(row["price"])
                else:
                    cost   = float(row["price"])
                    asking = float(list_price) if list_price else None

                # ── Get or create card_variant ────────────────────────────────
                cur.execute("""
                    SELECT id FROM card_variants
                    WHERE card_id = %s AND variant_type = %s AND finish = %s
                """, (str(row["card_id"]), variant_type, finish))
                v_row = cur.fetchone()
                if v_row:
                    variant_id = str(v_row["id"])
                else:
                    cur.execute("""
                        INSERT INTO card_variants
                            (card_id, variant_type, finish, is_special)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (card_id, variant_type, finish)
                        DO UPDATE SET is_special = EXCLUDED.is_special
                        RETURNING id
                    """, (str(row["card_id"]), variant_type, finish, is_special))
                    variant_id = str(cur.fetchone()["id"])

                # ── Insert inventory row ──────────────────────────────────────
                cur.execute("""
                    INSERT INTO inventory
                        (card_id, purchase_id, condition, is_graded, quantity,
                         cost_basis, asking_price, notes, acquired_at, variant_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    str(row["card_id"]),
                    purchase_id,
                    row["condition"],
                    False,
                    int(row["quantity"]),
                    cost,
                    asking,
                    None,
                    order_date,
                    variant_id,
                ))

                # ── Upsert market price if available ──────────────────────────
                market      = row.get("market_price")
                market_date = row.get("market_price_date")

                if not market and not is_ebay:
                    # TCGPlayer: fetch from API as fallback (best effort)
                    try:
                        from utils.pokemon_api import get_market_price_from_api
                        market = get_market_price_from_api(
                            str(row["card_id"]), row["condition"]
                        )
                    except Exception:
                        pass

                if market:
                    cur.execute("""
                        INSERT INTO market_prices
                            (variant_id, condition, market_price, source, updated_at)
                        VALUES (%s, %s, %s, %s,
                            COALESCE(TO_DATE(%s, 'YYYY/MM/DD'), NOW()))
                        ON CONFLICT (variant_id, condition)
                        DO UPDATE SET
                            market_price = EXCLUDED.market_price,
                            source       = EXCLUDED.source,
                            updated_at   = EXCLUDED.updated_at
                        WHERE market_prices.updated_at < EXCLUDED.updated_at
                    """, (
                        variant_id, row["condition"],
                        float(market), "pokemontcg",
                        str(market_date) if market_date else None,
                    ))

                # ── Print progress ────────────────────────────────────────────
                if is_ebay:
                    print(f"  ✓ {row['quantity']}x {row['card_name']} "
                          f"[{row['condition']}] asking ${asking:.2f}"
                          + (f" | market ${float(market):.2f}" if market else ""))
                else:
                    print(f"  ✓ {row['quantity']}x {row['card_name']} "
                          f"[{row['condition']}] cost ${cost:.2f}"
                          + (f" → list ${asking:.2f}" if asking else ""))
                pushed += 1

        # ── Mark batch as processed ───────────────────────────────────────────
        cur.execute("""
            UPDATE staging
            SET status = 'processed', updated_at = NOW()
            WHERE import_batch = %s AND status = 'approved'
        """, (batch_id,))

        conn.commit()

    except Exception as e:
        conn.rollback()
        print(f"\n❌ Error pushing batch {batch_id}: {e}")
        raise
    finally:
        conn.close()

    print(f"\n✓ {pushed} card(s) pushed to inventory.")
    print("Run  python3 main.py --stock  to see your inventory.\n")

# ----------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------

def _fmt_dt(dt) -> str:
    if not dt:
        return "—"
    if hasattr(dt, "strftime"):
        return dt.strftime("%Y-%m-%d %H:%M")
    return str(dt)[:16]


def approve_all_staging():
    """Approve ALL pending staging rows and push to inventory without review."""
    from db.connection import db_cursor

    # Get all pending batches
    with db_cursor() as cur:
        cur.execute("""
            UPDATE staging SET status = 'approved', updated_at = NOW()
            WHERE status = 'pending' AND card_id IS NOT NULL
        """)
        cur.execute("""
            UPDATE staging SET status = 'skipped', updated_at = NOW()
            WHERE status = 'pending' AND card_id IS NULL
        """)

    with db_cursor() as cur:
        cur.execute("""
            SELECT DISTINCT import_batch FROM staging
            WHERE status = 'approved'
            AND import_batch NOT IN (
                SELECT DISTINCT import_batch FROM staging
                WHERE status = 'processed'
            )
            ORDER BY import_batch DESC
        """)
        batches = [r["import_batch"] for r in cur.fetchall()]

    if not batches:
        print("No staged items to approve.")
        return

    print(f"Approving {len(batches)} batch(es)...")
    for batch_id in batches:
        _push_batch(batch_id)


def approve_order(order_number: str):
    """Approve a specific order and push to inventory."""
    from db.connection import db_cursor

    # Partial match on order number
    with db_cursor() as cur:
        cur.execute("""
            SELECT DISTINCT order_number FROM staging
            WHERE order_number ILIKE %s
              AND status = 'pending'
        """, (f"%{order_number}%",))
        matches = [r["order_number"] for r in cur.fetchall()]

    if not matches:
        print(f"No pending staging rows found for order: {order_number}")
        return

    for order_num in matches:
        print(f"Approving order {order_num}...")
        with db_cursor() as cur:
            cur.execute("""
                UPDATE staging SET status = 'approved', updated_at = NOW()
                WHERE order_number = %s AND status = 'pending'
                  AND card_id IS NOT NULL
            """, (order_num,))
            cur.execute("""
                UPDATE staging SET status = 'skipped', updated_at = NOW()
                WHERE order_number = %s AND status = 'pending'
                  AND card_id IS NULL
            """, (order_num,))

        _push_batch_by_order(order_num)


def _push_batch_by_order(order_number: str):
    """Push approved staging rows for a specific order to inventory."""
    from db.connection import (db_cursor, insert_inventory,
                               get_or_create_variant, upsert_market_price)
    import json

    with db_cursor() as cur:
        cur.execute("""
            SELECT s.*
            FROM staging s
            WHERE s.order_number = %s AND s.status = 'approved'
        """, (order_number,))
        rows = cur.fetchall()

    if not rows:
        return

    # Create purchase record if needed
    with db_cursor() as cur:
        cur.execute("""
            SELECT id FROM purchases WHERE reference_id = %s
        """, (order_number,))
        existing = cur.fetchone()

    if not existing:
        with db_cursor() as cur:
            cur.execute("""
                INSERT INTO purchases
                    (reference_id, source, purchase_type, total_cost,
                     card_count, purchased_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (
                order_number,
                rows[0].get("source", "tcgplayer"),
                "tcgplayer_order",
                sum(float(r["price"]) * int(r["quantity"]) for r in rows),
                sum(int(r["quantity"]) for r in rows),
                rows[0]["order_date"],
            ))
            purchase_id = str(cur.fetchone()["id"])
    else:
        purchase_id = str(existing["id"])

    SPECIAL_PATTERNS = {"Cosmos Holo", "Master Ball Pattern", "Poke Ball Pattern",
                        "Cracked Ice Holo", "Galaxy Holo"}

    for row in rows:
        list_price = row.get("override_price") or row.get("calculated_price")
        foil_type    = row.get("foil_type")
        foil_pattern = row.get("foil_pattern")
        variant_type = foil_pattern or foil_type or "Non-Holo"
        finish       = foil_type or "Non-Holo"
        is_special   = variant_type in SPECIAL_PATTERNS

        variant_id = get_or_create_variant(
            card_id=str(row["card_id"]),
            variant_type=variant_type,
            finish=finish,
            is_special=is_special,
        )
        insert_inventory(
            card_id     = str(row["card_id"]),
            purchase_id = purchase_id,
            condition   = row["condition"],
            quantity    = int(row["quantity"]),
            cost_basis  = float(row["price"]),
            asking_price= float(list_price) if list_price else None,
            acquired_at = row["order_date"],
            variant_id  = variant_id,
        )

    print(f"  Pushed {len(rows)} rows to inventory")
