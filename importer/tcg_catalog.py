"""
importer/tcg_catalog.py
Mirror of the TCGplayer Pokemon catalogue (public data mirror tcgcsv.com) and the link from each
TCGplayer card product to our exact card variant.

  python main.py --tcg-catalog-sync [--force] [--dry-run]

  1. Groups (sets) and products are copied into tcg_catalog_groups / tcg_catalog_products
     (sql/tcg_catalog.sql). Only groups whose modifiedOn changed are re-fetched (--force: all).
  2. Every card product (one with a printed number) is matched like an order line would be:
     set (alias or set name) + number + name -> our card, finish -> our variant. Exact matches are
     written to tcg_products (source 'catalogue'; never replaces a 'manual' link). Order lines are
     then re-matched, so they are found by TCGplayer product id.
  3. Prints which TCGplayer sets could not be tied to one of our sets (they need a
     tcgplayer_set_aliases row) and how many card products found no exact variant.
"""

import json
import re
import time
import urllib.request

from psycopg2.extras import execute_values

from db.connection import db_cursor
from importer.tcg_orders import Matcher, _split_name, log, rematch_unmatched
from importer.tcgplayer_html import (
    REVERSE_HOLO_OVERRIDE_PATTERNS, REVERSE_HOLO_PATTERN_SETS, COSMOS_TEXTURE_VARIANTS,
    _extract_foil_fields, _extract_foil_type, _load_foil_pattern_codes, _resolve_foil_pattern_code,
)
from utils.set_name_map import _strip_tcgplayer_prefix

BASE = "https://tcgcsv.com/tcgplayer/3"          # category 3 = Pokemon
PRINTINGS = ("Normal", "Holofoil", "Reverse Holofoil")


def _get(url: str) -> dict:
    last = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "CardBoardMasterMind/1.0"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except Exception as e:  # network hiccup: wait and retry
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"could not fetch {url}: {last}")


def _ext(product: dict, name: str):
    for e in product.get("extendedData") or []:
        if e.get("name") == name:
            return e.get("value")
    return None


def sync_catalog(force: bool = False, dry_run: bool = False) -> dict:
    groups = _get(f"{BASE}/groups")["results"]
    with db_cursor() as cur:
        cur.execute("SELECT group_id, modified_on FROM tcg_catalog_groups")
        have = {r["group_id"]: r["modified_on"] for r in cur.fetchall()}
    stats = {"groups": len(groups), "fetched": 0, "products": 0, "cards": 0}
    for g in groups:
        gid = g["groupId"]
        mod = g.get("modifiedOn")
        if not force and gid in have and have[gid] is not None and mod and have[gid].replace(tzinfo=None).isoformat()[:19] >= mod[:19]:
            continue
        products = _get(f"{BASE}/{gid}/products")["results"]
        prices = _get(f"{BASE}/{gid}/prices")["results"]
        sub = {}
        for p in prices:
            sub.setdefault(p["productId"], set()).add(p.get("subTypeName") or "Normal")
        rows = []
        for p in products:
            number = _ext(p, "Number")
            rows.append((p["productId"], gid, p["name"], number, _ext(p, "Rarity"), bool(number),
                         sorted(sub.get(p["productId"], [])), p.get("url"), p.get("imageUrl"), p.get("modifiedOn")))
        stats["fetched"] += 1
        stats["products"] += len(rows)
        stats["cards"] += sum(1 for r in rows if r[5])
        if not dry_run:
            with db_cursor() as cur:
                cur.execute("SET LOCAL statement_timeout = '120s'")
                cur.execute("""INSERT INTO tcg_catalog_groups (group_id, name, abbreviation, published_on, modified_on)
                               VALUES (%s, %s, %s, %s, %s)
                               ON CONFLICT (group_id) DO UPDATE SET name = EXCLUDED.name, abbreviation = EXCLUDED.abbreviation,
                                   published_on = EXCLUDED.published_on, modified_on = EXCLUDED.modified_on, synced_at = now()""",
                            (gid, g["name"], g.get("abbreviation"), (g.get("publishedOn") or "")[:10] or None, mod))
                if rows:
                    execute_values(cur, """INSERT INTO tcg_catalog_products (product_id, group_id, name, number, rarity, is_card, printings, url, image_url, modified_on)
                                           VALUES %s ON CONFLICT (product_id) DO UPDATE SET group_id = EXCLUDED.group_id, name = EXCLUDED.name,
                                               number = EXCLUDED.number, rarity = EXCLUDED.rarity, is_card = EXCLUDED.is_card,
                                               printings = EXCLUDED.printings, url = EXCLUDED.url, image_url = EXCLUDED.image_url,
                                               modified_on = EXCLUDED.modified_on""", rows)
        time.sleep(0.15)    # be polite to the free mirror
    log(f"catalogue: {stats['fetched']} of {stats['groups']} sets fetched, {stats['products']} products ({stats['cards']} cards)")
    return stats


def _line_for(prod: dict, printing: str, set_label: str, pattern_codes: dict) -> dict:
    """Same finish reading as an order line: the name's parentheses + 'Near Mint <printing>'."""
    cond = "Near Mint" if printing == "Normal" else f"Near Mint {printing}"
    name, number, variant = _split_name(prod["name"], pattern_codes)
    foil_type, foil_pattern = _extract_foil_fields(variant, _extract_foil_type(cond))
    texture = None
    if (foil_pattern or "").strip().lower() in COSMOS_TEXTURE_VARIANTS:
        texture, foil_pattern = "cosmos", None
    code = _resolve_foil_pattern_code(foil_pattern, pattern_codes)
    if code:
        foil_pattern = code
    if _strip_tcgplayer_prefix(set_label) in REVERSE_HOLO_PATTERN_SETS and foil_pattern in REVERSE_HOLO_OVERRIDE_PATTERNS:
        foil_type = "reverse_holo"
    printed = (prod.get("number") or "").split("/")[0].strip() or number
    return {"product_id": prod["product_id"], "printing": printing, "set_label": set_label, "card_name": name,
            "card_number": printed, "rarity": prod.get("rarity"), "foil_type": foil_type,
            "foil_pattern": foil_pattern, "texture": texture, "product_slug": prod.get("url")}


def map_catalog(dry_run: bool = False) -> dict:
    matcher = Matcher()
    codes = _load_foil_pattern_codes()
    with db_cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '120s'")
        cur.execute("""SELECT p.product_id, p.name, p.number, p.rarity, p.printings, p.url, g.name AS group_name, g.group_id
                       FROM tcg_catalog_products p JOIN tcg_catalog_groups g USING (group_id) WHERE p.is_card""")
        prods = [dict(r) for r in cur.fetchall()]
    stats = {"card_products": len(prods), "linked": 0, "already": 0, "card_only": 0, "unmatched": 0}
    unmapped_sets, writes, set_ids = {}, [], {}
    for p in prods:
        sid = matcher._set_id(p["group_name"])
        if not sid:
            unmapped_sets[p["group_name"]] = unmapped_sets.get(p["group_name"], 0) + 1
            stats["unmatched"] += 1
            continue
        set_ids[p["group_id"]] = sid
        for printing in [x for x in (p["printings"] or ["Normal"]) if x in PRINTINGS]:
            if (p["product_id"], printing) in matcher.product_map:
                stats["already"] += 1
                continue
            card_id, variant_id, status, source = matcher.resolve(_line_for(p, printing, p["group_name"], codes))
            if status == "matched" and source == "set_number":
                writes.append((p["product_id"], printing, variant_id, card_id, p["name"], p["group_name"], p["url"]))
            else:
                stats["card_only" if status == "card_only" else "unmatched"] += 1
    stats["linked"] = len(writes)
    if not dry_run:
        with db_cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '120s'")
            if writes:
                execute_values(cur, """INSERT INTO tcg_products (product_id, printing, variant_id, card_id, name, set_label, slug, source)
                                       VALUES %s ON CONFLICT (product_id, printing) DO NOTHING""",
                               [w + ("catalogue",) for w in writes], template="(%s,%s,%s,%s,%s,%s,%s,%s)")
            for gid, sid in set_ids.items():
                cur.execute("UPDATE tcg_catalog_groups SET card_set_id = %s WHERE group_id = %s", (sid, gid))
        n = rematch_unmatched()
        log(f"catalogue: {stats['linked']} product/finish links added, {n} order line(s) re-matched")
    stats["unmapped_sets"] = dict(sorted(unmapped_sets.items(), key=lambda x: -x[1]))
    return stats


def run(force: bool = False, dry_run: bool = False):
    s = sync_catalog(force=force, dry_run=dry_run)
    if dry_run and s["fetched"]:
        log("dry run: nothing written, so the matching step is skipped")
        return
    m = map_catalog(dry_run=dry_run)
    print(json.dumps({k: v for k, v in m.items() if k != "unmapped_sets"}))
    if m["unmapped_sets"]:
        print(f"TCGplayer sets with no matching set of ours ({len(m['unmapped_sets'])}), by number of cards:")
        for name, n in list(m["unmapped_sets"].items())[:40]:
            print(f"  {n:>4}  {name}")
