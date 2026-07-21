"""
importer/ebay_listing_sync.py — DB -> eBay price/quantity listing sync.

Two-phase, per docs/plans/ebay-listing-sync.md:
  --recalc   DB-only. Runs the pricing pipeline for every (variant, listing)
             pair in scope and writes platform_listings.list_price when it
             changed. Never touches eBay.
  --push     Reads platform_listings + inventory and calls eBay's
             ReviseFixedPriceItem to make the live listing match. (not yet
             implemented in this pass — see cmd_push stub below)

Gating (checked in this order, ALL must pass): platform_sync_status kill
switch -> platform_listings.sync_enabled -> platform_listings.status =
'active' (the last clause hard-excludes 'draft' / 'delisted' manual
statuses regardless of sync_enabled — see Step 0 finding #4 in the plan
doc). The 'do_not_sync' status value that finding #4 originally discussed
has since been removed from the app entirely (2026-07-21, Fei's decision —
sync_enabled alone covers that use case); this status='active' check is
unaffected since it was never keyed to that specific value.

Pricing pipeline (locked design, revised per Step 0 findings #5/#8):
  1. card_pricing_overrides.list_price       — absolute, beats floors too
  2. card_type_mapping (rarity + variant_key, most-specific-first)
  3a. price_tiers bracket + bumps (set_pricing_config.tier_bump, low-stock)
  3b. ultra_rare_rule formula path (no bumps)
  4. set_pricing_config.price_multiplier
  6. floors (raise-only, skipped if price came from step 1)
  7. rounding (skipped if price came verbatim from an untouched tier row)
  8. fallbacks (no market price / stale market price)

--dry-run MUST flag any card whose tier resolved via the wildcard
card_type_mapping row (specificity 0) — see Step 0 finding #8. Unmapped
rarities (Double Rare, Shiny Rare, etc.) are deliberately NOT seeded;
Fei configures them via the Configuration UI as they come up.
"""

from decimal import Decimal, ROUND_HALF_UP

from db.connection import db_cursor
from importer.ebay_auth import get_account_name

TIER_TYPES = ("common", "holo", "reverse_holo", "ultra_rare_rule")


# ══════════════════════════════════════════════════════════════════════════════
# Scope resolution
# ══════════════════════════════════════════════════════════════════════════════

def _resolve_scope(cur, platform: str, account: str = None,
                    item_id: str = None, card_query: str = None):
    """
    Returns a list of platform_listings rows (as dicts) in scope, already
    filtered through the sync-status kill switch, sync_enabled, and
    status='active'. `item_id` scopes to one eBay listing (by listing_id);
    `card_query` scopes to one card (by name, case-insensitive substring)
    across whichever enabled listings hold it.
    """
    cur.execute(
        "SELECT sync_enabled FROM platform_sync_status WHERE platform = %s AND account IS NULL",
        (platform,),
    )
    row = cur.fetchone()
    if row and not row["sync_enabled"]:
        return []  # platform-wide kill switch engaged

    if account:
        cur.execute(
            "SELECT sync_enabled FROM platform_sync_status WHERE platform = %s AND account = %s",
            (platform, account),
        )
        row = cur.fetchone()
        if row and not row["sync_enabled"]:
            return []  # account-level kill switch engaged

    where = ["pl.platform = %s", "pl.sync_enabled = true", "pl.status = 'active'"]
    params = [platform]

    if account:
        where.append("pl.account = %s")
        params.append(account)

    if item_id:
        where.append("pl.listing_id = %s")
        params.append(item_id)

    if card_query:
        where.append("cm.name ILIKE %s")
        params.append(f"%{card_query}%")

    cur.execute(
        f"""
        SELECT pl.*, cm.name AS card_name, cm.rarity AS card_rarity,
               cm.card_number, cm.card_number_numeric,
               cv.variant_key
        FROM platform_listings pl
        JOIN card_variants cv ON pl.variant_id = cv.id
        JOIN card_master cm   ON cv.card_id = cm.id
        WHERE {' AND '.join(where)}
        ORDER BY pl.listing_id, cm.card_number_numeric
        """,
        params,
    )
    return cur.fetchall()


# ══════════════════════════════════════════════════════════════════════════════
# card_type_mapping resolver (Step 0 finding #8)
# ══════════════════════════════════════════════════════════════════════════════

def resolve_tier_card_type(cur, rarity: str, variant_key: str,
                            platform: str, account: str = None) -> dict:
    """
    Returns {"tier_card_type": str, "specificity": int, "wildcard": bool}.
    specificity 0 == matched only the all-NULL wildcard row — dry-run must
    flag this (unmapped rarity, e.g. a new set's rarity string that hasn't
    been added to card_type_mapping yet).
    """
    cur.execute(
        """
        SELECT tier_card_type,
               (rarity IS NOT NULL)::int + (variant_key IS NOT NULL)::int AS specificity
        FROM card_type_mapping
        WHERE platform = %s
          AND (account = %s OR account IS NULL)
          AND (rarity IS NULL OR rarity = %s)
          AND (variant_key IS NULL OR %s LIKE variant_key)
        ORDER BY specificity DESC, priority DESC, created_at DESC
        LIMIT 1
        """,
        (platform, account, rarity, variant_key),
    )
    row = cur.fetchone()
    if row is None:
        # Should not happen — the all-NULL wildcard row always matches —
        # but don't silently misprice if it somehow does.
        return {"tier_card_type": None, "specificity": -1, "wildcard": True}
    return {
        "tier_card_type": row["tier_card_type"],
        "specificity": row["specificity"],
        "wildcard": row["specificity"] == 0,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Pricing pipeline
# ══════════════════════════════════════════════════════════════════════════════

def _round_up_charm(price: float) -> float:
    """Round up to the nearest .49/.99 ending."""
    price = Decimal(str(price))
    whole = int(price)
    if price <= whole + Decimal("0.49"):
        return float(whole + Decimal("0.49"))
    return float(whole + Decimal("0.99"))


def resolve_price(cur, card_id: str, variant_id: str, rarity: str,
                   variant_key: str, set_id: str, listing: dict,
                   platform: str, account: str) -> dict:
    """
    Runs the full pricing pipeline for one (card, listing). Returns:
      {list_price, rule_used, breakdown, wildcard_tier, clamped,
       market_price, market_age_days, skip_reason}
    `skip_reason` is set (and list_price is None) when there's nothing
    sane to price against (no market price + no floor).
    """
    breakdown = []

    # ── Step 1: card-level override (absolute, skips everything else) ──────
    cur.execute(
        "SELECT list_price FROM card_pricing_overrides WHERE card_id = %s AND platform = %s "
        "AND (account = %s OR account IS NULL) ORDER BY account NULLS LAST LIMIT 1",
        (card_id, platform, account),
    )
    override = cur.fetchone()
    if override:
        price = float(override["list_price"])
        return {
            "list_price": price, "list_price_bump_forced": price, "rule_used": "card_override",
            "breakdown": f"Manual override: ${override['list_price']:.2f}",
            "wildcard_tier": False, "clamped": False,
            "market_price": None, "market_age_days": None, "skip_reason": None,
        }

    # ── Step 2: tier classification ─────────────────────────────────────────
    tier = resolve_tier_card_type(cur, rarity, variant_key, platform, account)
    tier_card_type = tier["tier_card_type"]
    breakdown.append(f"rarity={rarity!r} variant_key={variant_key!r} -> tier={tier_card_type}"
                      + (" [WILDCARD — unmapped rarity]" if tier["wildcard"] else ""))

    # ── Market price (+ staleness) ──────────────────────────────────────────
    cur.execute(
        "SELECT market_price, updated_at FROM market_prices WHERE variant_id = %s "
        "ORDER BY updated_at DESC LIMIT 1",
        (variant_id,),
    )
    mp_row = cur.fetchone()
    market_price = float(mp_row["market_price"]) if mp_row and mp_row["market_price"] else None
    market_age_days = None
    if mp_row and mp_row["updated_at"]:
        from datetime import datetime, timezone
        market_age_days = (datetime.now(timezone.utc) - mp_row["updated_at"]).days

    # ── set_pricing_config (multiplier, floors, tier_bump, ultra_rare_rule) ─
    cur.execute(
        "SELECT * FROM set_pricing_config WHERE set_id = %s AND platform = %s "
        "AND (account = %s OR account IS NULL) ORDER BY account NULLS LAST LIMIT 1",
        (set_id, platform, account),
    )
    set_config = cur.fetchone()

    clamped = False

    if market_price is None:
        # ── Step 8 fallback: no market price at all ─────────────────────────
        floor = _resolve_floor(set_config, tier_card_type, listing)
        if floor is None:
            return {
                "list_price": None, "list_price_bump_forced": None,
                "rule_used": "skip", "breakdown": "no market price, no floor",
                "wildcard_tier": tier["wildcard"], "clamped": False,
                "market_price": None, "market_age_days": None,
                "skip_reason": "no market_prices row and no applicable floor",
            }
        breakdown.append(f"no market price -> using floor ${floor:.2f}")
        return {
            "list_price": floor, "list_price_bump_forced": floor, "rule_used": "floor_no_market",
            "breakdown": " | ".join(breakdown),
            "wildcard_tier": tier["wildcard"], "clamped": False,
            "market_price": None, "market_age_days": None, "skip_reason": None,
        }

    if tier_card_type == "ultra_rare_rule":
        # ── 3b. Formula path — no bumps, no with/without-bump ambiguity ──────
        rule = (set_config or {}).get("ultra_rare_rule", "tier")
        if rule == "multiplier":
            mult = float((set_config or {}).get("ultra_rare_multiplier") or 2.0)
            plus = float((set_config or {}).get("ultra_rare_plus") or 1.0)
            price = market_price * mult + plus
            price_bump_forced = price  # no bumps apply on the formula path
            rule_used = "ultra_rare_multiplier"
            breakdown.append(f"formula: ${market_price:.2f} x {mult} + {plus} = ${price:.2f} "
                              "(bump n/a: formula-priced)")
        else:
            base_price, tb = _apply_tiers(cur, market_price, "ultra_rare_rule", platform, account)
            price, price_bump_forced, tb2, clamp = _apply_bumps(
                cur, base_price, set_config, listing, platform, account)
            clamped = clamped or clamp
            rule_used = "tier"
            breakdown.append(tb)
            breakdown.append(tb2)
    else:
        # ── 3a. Tier path + bumps ────────────────────────────────────────────
        base_price, tb = _apply_tiers(cur, market_price, tier_card_type, platform, account)
        breakdown.append(tb)
        price, price_bump_forced, tb2, clamp = _apply_bumps(
            cur, base_price, set_config, listing, platform, account)
        clamped = clamped or clamp
        breakdown.append(tb2)
        rule_used = "tier"

    # ── Step 4: set-level multiplier (applied identically to both variants) ─
    multiplier = float((set_config or {}).get("price_multiplier") or 1.0)
    price_before_mult = price
    if multiplier != 1.0:
        price = price * multiplier
        price_bump_forced = price_bump_forced * multiplier
        rule_used = "set_multiplier"
        breakdown.append(f"x{multiplier} multiplier: ${price_before_mult:.2f} -> ${price:.2f}")

    # ── Step 6: floors (raise-only) ─────────────────────────────────────────
    floor = _resolve_floor(set_config, tier_card_type, listing)
    if floor is not None and price < floor:
        breakdown.append(f"floor ${floor:.2f} applied (was ${price:.2f})")
        price = floor
        rule_used = "floor"
    if floor is not None and price_bump_forced < floor:
        price_bump_forced = floor

    # ── Step 7: rounding — only if price didn't come verbatim from a tier row
    needs_rounding = rule_used in ("ultra_rare_multiplier", "set_multiplier")
    if needs_rounding:
        rounded = _round_up_charm(price)
        if rounded != price:
            breakdown.append(f"rounded ${price:.2f} -> ${rounded:.2f}")
        price = rounded
        price_bump_forced = _round_up_charm(price_bump_forced)

    return {
        "list_price": round(price, 2),
        # counterfactual: what price would be if the low-stock bump were
        # forced on, regardless of whether it's currently active. Used by
        # the never-lower guard to detect a legitimate bump-expiry decrease
        # (old_price matches this, new_price matches list_price) vs. a real
        # price drop that needs --allow-decreases.
        "list_price_bump_forced": round(price_bump_forced, 2),
        "rule_used": rule_used,
        "breakdown": " | ".join(breakdown),
        "wildcard_tier": tier["wildcard"], "clamped": clamped,
        "market_price": market_price, "market_age_days": market_age_days,
        "skip_reason": None,
    }


def _apply_tiers(cur, market_price: float, tier_card_type: str,
                  platform: str, account: str):
    cur.execute(
        "SELECT list_price, market_price_max FROM price_tiers WHERE platform = %s AND card_type = %s "
        "AND (account = %s OR account IS NULL) AND market_price_max >= %s "
        "ORDER BY account NULLS LAST, market_price_max ASC LIMIT 1",
        (platform, tier_card_type, account, market_price),
    )
    row = cur.fetchone()
    if row:
        return float(row["list_price"]), f"market ${market_price:.2f} -> tier ${row['list_price']:.2f}"

    # Above all tiers -> formula fallback, will get clamped by caller marking it
    price = round(market_price * 2 + 1.0, 2)
    return price, f"market ${market_price:.2f} above top tier -> formula ${price:.2f} [CLAMPED — report in dry-run]"


def _apply_bumps(cur, price: float, set_config: dict, listing: dict,
                  platform: str, account: str):
    """
    Returns (price_with_actual_bumps, price_with_low_stock_bump_forced_on,
    breakdown_text, clamped). tier_bump is unconditional (applies to both
    variants identically); low_stock_bump is the only conditional one —
    the "forced" variant always includes it, used by the never-lower guard
    to detect a legitimate bump-expiry price decrease.
    """
    tier_bump = float((set_config or {}).get("tier_bump") or 0)
    low_stock_bump_amount = 0.0
    low_stock_bump_active = False
    threshold = 8

    if listing.get("template_id"):
        cur.execute("SELECT low_stock_threshold, low_stock_bump FROM listing_templates WHERE id = %s",
                    (listing["template_id"],))
        tmpl = cur.fetchone()
        if tmpl:
            threshold = tmpl["low_stock_threshold"] or 8
            low_stock_bump_amount = float(tmpl["low_stock_bump"] or 0)
            available = listing["quantity_limit"] - listing.get("quantity_listed", 0) \
                if listing.get("quantity_limit") is not None else None
            if available is not None and available < threshold:
                low_stock_bump_active = True

    actual_bump = tier_bump + (low_stock_bump_amount if low_stock_bump_active else 0)
    forced_bump = tier_bump + low_stock_bump_amount

    price_actual = price + actual_bump
    price_forced = price + forced_bump

    text = (f"+ tier_bump {tier_bump} + low_stock_bump "
            f"{low_stock_bump_amount if low_stock_bump_active else 0} "
            f"(active={low_stock_bump_active}) = ${price_actual:.2f}")
    return price_actual, price_forced, text, False


def _resolve_floor(set_config: dict, tier_card_type: str, listing: dict):
    floor_field = {
        "common": "common_floor", "holo": "holo_floor", "reverse_holo": "reverse_holo_floor",
    }.get(tier_card_type)

    candidates = []
    if listing.get("base_price") is not None:
        candidates.append(float(listing["base_price"]))
    # template.base_price is checked by the caller passing it in via listing dict
    # (listing["_template_base_price"]) when available.
    if listing.get("_template_base_price") is not None:
        candidates.append(float(listing["_template_base_price"]))
    if set_config and floor_field and set_config.get(floor_field):
        candidates.append(float(set_config[floor_field]))

    return max(candidates) if candidates else None


# ══════════════════════════════════════════════════════════════════════════════
# --recalc
# ══════════════════════════════════════════════════════════════════════════════

def recalc(account_num: int = 1, item_id: str = None, card_query: str = None,
           dry_run: bool = False, quiet: bool = False, allow_decreases: bool = False,
           platform: str = "ebay"):
    account = get_account_name(account_num)

    with db_cursor() as cur:
        listings = _resolve_scope(cur, platform, account, item_id, card_query)

        if not listings:
            print("No listings in scope (check sync_enabled / platform_sync_status / status='active').")
            return

        # attach template base_price where present
        template_ids = {l["template_id"] for l in listings if l.get("template_id")}
        templates = {}
        if template_ids:
            cur.execute("SELECT id, base_price FROM listing_templates WHERE id = ANY(%s::uuid[])",
                        ([str(t) for t in template_ids],))
            templates = {t["id"]: t for t in cur.fetchall()}

        n_updated = n_skipped = n_wildcard = n_unchanged = n_guarded = 0

        for listing in listings:
            if listing.get("template_id") in templates:
                listing["_template_base_price"] = templates[listing["template_id"]]["base_price"]

            cur.execute("SELECT card_id, set_id FROM card_master WHERE id = "
                        "(SELECT card_id FROM card_variants WHERE id = %s)", (listing["variant_id"],))
            cm = cur.fetchone()

            result = resolve_price(
                cur, card_id=cm["card_id"], variant_id=listing["variant_id"],
                rarity=listing["card_rarity"], variant_key=listing["variant_key"],
                set_id=cm["set_id"], listing=listing, platform=platform, account=account,
            )

            label = f"{listing['listing_id']} / {listing.get('card_name','?')} #{listing.get('card_number','?')}"

            if result["skip_reason"]:
                n_skipped += 1
                if not quiet:
                    print(f"  [SKIP] {label}: {result['skip_reason']}")
                continue

            new_price = result["list_price"]
            old_price = float(listing["list_price"])

            if result["wildcard_tier"]:
                n_wildcard += 1
                print(f"  [WILDCARD TIER — unmapped rarity {listing['card_rarity']!r}] {label}: "
                      f"${old_price:.2f} -> ${new_price:.2f} ({result['breakdown']})")

            if abs(new_price - old_price) < 0.005:
                n_unchanged += 1
                continue

            if new_price < old_price and not allow_decreases:
                # Never-lower guard — EXCEPT a legitimate low-stock-bump
                # expiry: allowed iff the OLD stored price matches what
                # today's calculation would be WITH the low-stock bump
                # forced on (i.e. old_price was set by a prior recalc while
                # the bump was active), and the NEW price is today's actual
                # (bump-off) value. Anything else (e.g. market price itself
                # dropped) stays guarded.
                bump_expiry = (
                    result["list_price_bump_forced"] is not None
                    and abs(old_price - result["list_price_bump_forced"]) < 0.005
                )
                if not bump_expiry:
                    n_guarded += 1
                    if not quiet:
                        print(f"  [GUARDED] {label}: ${old_price:.2f} -> ${new_price:.2f} blocked "
                              f"(use --allow-decreases). {result['breakdown']}")
                    continue
                if not quiet:
                    print(f"  [BUMP EXPIRY — auto-permitted] {label}: ${old_price:.2f} -> ${new_price:.2f}")

            n_updated += 1
            if not quiet:
                clamp_note = " [CLAMPED]" if result["clamped"] else ""
                print(f"  {label}: ${old_price:.2f} -> ${new_price:.2f}{clamp_note}  ({result['breakdown']})")

            if not dry_run:
                cur.execute("UPDATE platform_listings SET list_price = %s WHERE id = %s",
                            (new_price, listing["id"]))

        summary = (f"[{'DRY-RUN ' if dry_run else ''}recalc] {len(listings)} listing(s) in scope: "
                   f"{n_updated} updated, {n_unchanged} unchanged, {n_guarded} guarded, "
                   f"{n_skipped} skipped, {n_wildcard} wildcard-tier (flagged above)")
        print(summary)


def _compute_desired_qty(cur, listing: dict) -> int:
    """MIN(quantity_available, COALESCE(card_override.quantity_limit,
    listing.quantity_limit, template.default_quantity_limit, 24))."""
    cur.execute(
        "SELECT condition FROM ebay_listing_map WHERE item_id = %s AND variation_name = %s",
        (listing["listing_id"], listing["external_id"]),
    )
    row = cur.fetchone()
    condition = row["condition"] if row else None

    if condition:
        cur.execute(
            "SELECT COALESCE(SUM(quantity - quantity_sold), 0) AS avail FROM inventory "
            "WHERE variant_id = %s AND condition = %s AND is_graded = FALSE",
            (listing["variant_id"], condition),
        )
    else:
        cur.execute(
            "SELECT COALESCE(SUM(quantity - quantity_sold), 0) AS avail FROM inventory "
            "WHERE variant_id = %s AND is_graded = FALSE",
            (listing["variant_id"],),
        )
    quantity_available = cur.fetchone()["avail"]

    cur.execute(
        "SELECT quantity_limit FROM card_pricing_overrides WHERE card_id = "
        "(SELECT card_id FROM card_variants WHERE id = %s) AND platform = %s",
        (listing["variant_id"], listing["platform"]),
    )
    override_row = cur.fetchone()
    card_override_limit = override_row["quantity_limit"] if override_row else None

    template_default_limit = None
    if listing.get("template_id"):
        cur.execute("SELECT default_quantity_limit FROM listing_templates WHERE id = %s",
                    (listing["template_id"],))
        t = cur.fetchone()
        template_default_limit = t["default_quantity_limit"] if t else None

    cap = card_override_limit or listing.get("quantity_limit") or template_default_limit or 24
    return min(int(quantity_available), int(cap))


def _get_listing_kind(cur, listing: dict) -> str:
    if listing.get("template_id"):
        cur.execute("SELECT listing_kind FROM listing_templates WHERE id = %s", (listing["template_id"],))
        t = cur.fetchone()
        if t:
            return t["listing_kind"]
    return "variation"


def _humanize(value: str) -> str:
    return value.replace("_", " ").title() if value else ""


def _render_variation_name(cur, variant_id: str, template_id: str = None) -> str:
    """Renders a card's display name for VariationSpecificsSet via the
    template's name_format (default if no template): tokens {number},
    {number:pad}, {set_total} (= card_sets.total_cards), {name}, {suffix}.

    The suffix MUST distinguish foil_type — confirmed against real listings
    (e.g. item 334903449758): reverse-holo variants are always suffixed
    "Reverse Holo RH" even with no foil_pattern/stamp_type, because a plain
    holo and reverse-holo copy of the same card number commonly coexist in
    the same RH-to-UR listing and need distinct VariationSpecificsSet
    values. Without this, two different variants of the same card could
    render to an identical name.
    """
    cur.execute(
        """
        SELECT cm.name, cm.card_number, cs.total_cards,
               cv.foil_type, cv.foil_pattern, cv.stamp_type
        FROM card_variants cv
        JOIN card_master cm ON cv.card_id = cm.id
        JOIN card_sets cs ON cm.set_id = cs.id
        WHERE cv.id = %s
        """,
        (variant_id,),
    )
    row = cur.fetchone()

    name_format = "{number}/{set_total} {name} {suffix}"
    if template_id:
        cur.execute("SELECT name_format FROM listing_templates WHERE id = %s", (template_id,))
        t = cur.fetchone()
        if t and t.get("name_format"):
            name_format = t["name_format"]

    suffix_parts = []
    if row["foil_pattern"]:
        suffix_parts.append(_humanize(row["foil_pattern"]))
    if row["foil_type"] == "reverse_holo":
        suffix_parts.append("Reverse Holo RH")
    if row["stamp_type"]:
        suffix_parts.append(_humanize(row["stamp_type"]))
    suffix = " ".join(suffix_parts)
    number = row["card_number"] or ""
    set_total = row["total_cards"]
    padded = number.zfill(len(str(set_total))) if set_total and number.isdigit() else number

    return (
        name_format
        .replace("{number:pad}", padded)
        .replace("{number}", number)
        .replace("{set_total}", str(set_total) if set_total else "")
        .replace("{name}", row["name"])
        .replace("{suffix}", suffix)
    ).strip()


def _compute_insert_position(cur, variations, specific_name: str, item_id_: str,
                              promoted_variant_id: str, display_sort: str):
    """
    Where in VariationSpecificsSet's value list the promoted card's name
    belongs, per display_sort. Only 'card_number' ordering is implemented
    (the only display_sort value confirmed in use — Step 0 #7); 'alpha'
    and 'release_date' (reserved for the future themed-listings feature)
    fall back to append-at-end rather than guess an ordering.
    Returns an index into the current value list, or None to append.
    """
    from importer.ebay_variations_xml import get_specifics_set

    if display_sort != "card_number":
        return None

    existing_values = get_specifics_set(variations).get(specific_name, [])

    cur.execute(
        """
        SELECT elm.variation_name, cm.card_number_numeric
        FROM ebay_listing_map elm
        JOIN card_variants cv ON elm.variant_id = cv.id
        JOIN card_master cm ON cv.card_id = cm.id
        WHERE elm.item_id = %s
        """,
        (item_id_,),
    )
    known = {r["variation_name"]: r["card_number_numeric"] for r in cur.fetchall()}

    cur.execute(
        "SELECT card_number_numeric FROM card_master WHERE id = "
        "(SELECT card_id FROM card_variants WHERE id = %s)",
        (promoted_variant_id,),
    )
    promoted_row = cur.fetchone()
    promoted_num = promoted_row["card_number_numeric"] if promoted_row else None
    if promoted_num is None:
        return None

    for idx, val in enumerate(existing_values):
        other_num = known.get(val)
        if other_num is not None and other_num > promoted_num:
            return idx
    return None


def _push_single(cur, item_id_: str, listing: dict, account_num: int,
                  dry_run: bool, quiet: bool) -> bool:
    """Item-level (no <Variations> block) push — for single-card listings."""
    from importer.ebay_variations_xml import fetch_item, get_quantity_sold
    from importer.ebay import _post

    item = fetch_item(item_id_, account_num=account_num)
    qty_sold = get_quantity_sold(item)  # works on <Item> too — just looks for a SellingStatus child

    desired_available = _compute_desired_qty(cur, listing)
    qty_to_set = qty_sold + desired_available
    new_price = float(listing["list_price"])

    if not quiet:
        print(f"  [single] {item_id_}: price -> ${new_price:.2f}, qty -> {qty_to_set} "
              f"(sold={qty_sold} + avail={desired_available})")

    if dry_run:
        return True

    from importer.ebay_auth import get_user_token
    token = get_user_token(account_num=account_num)
    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<ReviseFixedPriceItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">
  <RequesterCredentials>
    <eBayAuthToken>{token}</eBayAuthToken>
  </RequesterCredentials>
  <Item>
    <ItemID>{item_id_}</ItemID>
    <StartPrice>{new_price:.2f}</StartPrice>
    <Quantity>{qty_to_set}</Quantity>
  </Item>
</ReviseFixedPriceItemRequest>"""
    _post("ReviseFixedPriceItem", xml, account_num=account_num)

    cur.execute("UPDATE platform_listings SET price_synced_at = now(), quantity_listed = %s WHERE id = %s",
                (desired_available, listing["id"]))
    return True


def _push_variation_listing(cur, item_id_: str, group_listings: list, account_num: int,
                             dry_run: bool, quiet: bool) -> bool:
    """
    Multi-variation push: reconcile price/qty on live variations that have
    an 'active' listing_card_assignments row, plus 250-cap promotion
    (delete a sold-out row, promote the highest-priority queued card).
    Returns True if pushed, False if nothing to do, None if skipped
    (no assignment intent built for this listing yet — per the locked
    design, push never touches a listing this tool hasn't onboarded).
    """
    from importer.ebay_variations_xml import (
        fetch_item, deep_copy_variations, get_quantity_sold,
        find_variation_by_specifics, set_variation_price_qty, mark_variation_deleted,
        add_variation_row, get_specifics_set, insert_specifics_value, build_revise_xml,
    )
    from importer.ebay import _post, _findall

    platform_listing_ids = [str(l["id"]) for l in group_listings]
    cur.execute(
        "SELECT * FROM listing_card_assignments WHERE platform_listing_id = ANY(%s::uuid[]) OR ebay_item_id = %s",
        (platform_listing_ids, item_id_),
    )
    assignments = cur.fetchall()

    if not assignments:
        if not quiet:
            print(f"  [SKIP] {item_id_}: no listing_card_assignments rows — assignment intent "
                  f"hasn't been built for this listing yet (Rollout step 2). Not touching it.")
        return None

    item = fetch_item(item_id_, account_num=account_num)
    variations = deep_copy_variations(item)  # QuantitySold read BEFORE strip; do that first
    quantity_sold_by_var = {
        id(v): get_quantity_sold(v) for v in _findall(variations, "Variation")
    }
    from importer.ebay_variations_xml import strip_selling_status
    strip_selling_status(variations)

    cur.execute("SELECT variant_id, variation_name, condition FROM ebay_listing_map WHERE item_id = %s",
                (item_id_,))
    var_map = {r["variant_id"]: r for r in cur.fetchall()}
    specifics_set = get_specifics_set(variations)
    specific_name = next(iter(specifics_set), None)

    listings_by_variant = {l["variant_id"]: l for l in group_listings}
    changes = []  # (listing, desired_available) pairs whose platform_listings row needs updating

    for a in [a for a in assignments if a["status"] == "active"]:
        listing = listings_by_variant.get(a["variant_id"])
        if not listing:
            continue  # active per DB, but not in this run's push scope
        mapped = var_map.get(a["variant_id"])
        if not mapped or specific_name is None:
            if not quiet:
                print(f"  [WARN] {item_id_}: no ebay_listing_map entry for variant {a['variant_id']} — skipping")
            continue
        var_el = find_variation_by_specifics(variations, specific_name, mapped["variation_name"])
        if var_el is None:
            if not quiet:
                print(f"  [WARN] {item_id_}: variation {mapped['variation_name']!r} not found live — "
                      f"mismatch, needs manual reconcile in Seller Hub")
            continue

        qty_sold = quantity_sold_by_var.get(id(var_el), 0)
        desired_available = _compute_desired_qty(cur, listing)
        qty_to_set = qty_sold + desired_available
        set_variation_price_qty(var_el, start_price=float(listing["list_price"]), quantity=qty_to_set)
        changes.append((listing, desired_available))
        if not quiet:
            print(f"    {mapped['variation_name']}: price -> ${float(listing['list_price']):.2f}, "
                  f"qty -> {qty_to_set} (sold={qty_sold} + avail={desired_available})")

    # ── 250-cap promotion ────────────────────────────────────────────────────
    promotions = []
    if len(assignments) > 250:
        queued = sorted((a for a in assignments if a["status"] == "queued"),
                         key=lambda a: a["priority_rank"])
        for a in (a for a in assignments if a["status"] == "active"):
            if not queued:
                break
            mapped = var_map.get(a["variant_id"])
            listing = listings_by_variant.get(a["variant_id"])
            if not mapped or not listing:
                continue
            var_el = find_variation_by_specifics(variations, specific_name, mapped["variation_name"])
            if var_el is None:
                continue
            if _compute_desired_qty(cur, listing) > 0:
                continue  # not sold out — never delete a row with stock

            promote = queued.pop(0)
            mark_variation_deleted(var_el)

            template_id = group_listings[0].get("template_id")
            display_sort = "card_number"
            if template_id:
                cur.execute("SELECT display_sort FROM listing_templates WHERE id = %s", (template_id,))
                t = cur.fetchone()
                if t and t.get("display_sort"):
                    display_sort = t["display_sort"]

            promoted_name = _render_variation_name(cur, promote["variant_id"], template_id)
            position = _compute_insert_position(cur, variations, specific_name, item_id_,
                                                 promote["variant_id"], display_sort)
            insert_specifics_value(variations, specific_name, promoted_name, position=position)
            # Approximation: the promoted card has no platform_listings row of
            # its own yet (it was queued, not live), so there's no per-card
            # quantity_limit/condition to look up precisely. Reuses the
            # sold-out listing's condition/quantity_limit/platform context as
            # the best available stand-in — reasonable since quantity_limit is
            # primarily a per-template/per-listing concept, not per-card, but
            # flagged here since it hasn't been exercised against real data.
            new_avail = _compute_desired_qty(cur, {
                **listing, "variant_id": promote["variant_id"], "external_id": "",
            }) if listing else 0
            add_variation_row(variations, {specific_name: promoted_name}, quantity=new_avail,
                               start_price=float(listing["list_price"]) if listing else 0.0)
            promotions.append((a, promote))
            if not quiet:
                print(f"    [PROMOTE] {mapped['variation_name']} (sold out) -> {promoted_name!r} "
                      f"at position {position if position is not None else 'end'}")

    if not changes and not promotions:
        if not quiet:
            print(f"  [SKIP] {item_id_}: no active assignments matched anything to push")
        return False

    if dry_run:
        return True

    xml = build_revise_xml(item_id_, variations, "ReviseFixedPriceItem", account_num=account_num)
    _post("ReviseFixedPriceItem", xml, account_num=account_num)

    for listing, desired_available in changes:
        cur.execute("UPDATE platform_listings SET price_synced_at = now(), quantity_listed = %s WHERE id = %s",
                    (desired_available, listing["id"]))
    for old_assignment, promoted_assignment in promotions:
        cur.execute("UPDATE listing_card_assignments SET status = 'sold_out_retained', updated_at = now() "
                    "WHERE id = %s", (old_assignment["id"],))
        cur.execute("UPDATE listing_card_assignments SET status = 'active', updated_at = now() "
                    "WHERE id = %s", (promoted_assignment["id"],))
    return True


def push(account_num: int = 1, item_id: str = None, card_query: str = None,
         dry_run: bool = False, quiet: bool = False, force: bool = False,
         platform: str = "ebay"):
    """
    eBay-writing half of the sync feature. Scope -> group by eBay item_id
    (one GetItem/ReviseFixedPriceItem per item, however many variants of it
    are in scope) -> per item, dispatch to _push_single or
    _push_variation_listing based on the listing's template listing_kind.
    NOTE: has not been exercised against a real listing yet — that requires
    Fei's Rollout steps 2-3 (backfill listing_card_assignments for a test
    listing, flip sync_enabled on) before there's anything real to push
    against. All DB-facing pieces (_compute_desired_qty, _get_listing_kind,
    _render_variation_name) and the XML helpers they call are independently
    testable/tested; the live eBay call path (fetch_item / build_revise_xml
    / _post) is not.
    """
    account = get_account_name(account_num)

    with db_cursor() as cur:
        listings = _resolve_scope(cur, platform, account, item_id, card_query)

        if not force:
            listings = [
                l for l in listings
                if l["price_synced_at"] is None or l["updated_at"] > l["price_synced_at"]
            ]

        if not listings:
            print("No listings with pending changes in scope (use --force to push anyway).")
            return

        groups = {}
        for l in listings:
            groups.setdefault(l["listing_id"], []).append(l)

        n_pushed = n_skipped_no_assignments = n_noop = n_errors = 0
        for grp_item_id, group_listings in groups.items():
            listing_kind = _get_listing_kind(cur, group_listings[0])
            try:
                if listing_kind == "single":
                    result = _push_single(cur, grp_item_id, group_listings[0], account_num, dry_run, quiet)
                else:
                    result = _push_variation_listing(cur, grp_item_id, group_listings, account_num, dry_run, quiet)
            except Exception as e:
                n_errors += 1
                print(f"  [ERROR] {grp_item_id}: {e}")
                continue

            if result is None:
                n_skipped_no_assignments += 1
            elif result:
                n_pushed += 1
            else:
                n_noop += 1

        print(f"[{'DRY-RUN ' if dry_run else ''}push] {len(groups)} item(s) in scope: "
              f"{n_pushed} pushed, {n_noop} no-op, "
              f"{n_skipped_no_assignments} skipped (no assignment intent yet), {n_errors} error(s)")
