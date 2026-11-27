"""
importer/tcg_orders.py
TCGplayer purchase-order tracker: reads the order-list pages Grok saves into
the Dropbox inbox and keeps tcg_orders / tcg_order_lines / tcg_order_refunds
up to date (tables: card-board-mastermind-WebInvManagement/sql/tcg_orders.sql).

  python main.py --tcg-orders-process [--dry-run]   # what Task Scheduler runs
  python main.py --tcg-orders-status                # counts, open orders

Inbox (default C:\\Users\\Fei\\Dropbox\\tcg_orders\\inbox, env TCG_ORDERS_INBOX):
  orders_<YYYYMMDD>_<window>_page<N>.html   the order-list pages (any window label)
  grab_ok_<YYYYMMDD>_<window>.txt           Grok's success note (orders seen / saved)
  run_failed_<YYYYMMDD>.txt                 Grok's failure note

What it does with them:
  * Each page holds many order blocks. Every order is upserted by order number.
    TCGplayer's own label becomes our status: "Shipping Not Confirmed" ->
    not_shipped, "Shipped Without Tracking" -> shipped_no_tracking, "Shipped With
    Tracking:" -> shipped_tracking (+ tracking number/link), "Canceled" -> cancelled.
    TCGplayer never shows "Delivered", so an order is "received" only when its
    order number is already in purchases/staging (marked automatically) or you
    mark it yourself.
  * Card lines are read from the item rows (TCGplayer product id from the
    thumbnail address) and matched to our cards/variants in memory: a learned
    product -> variant map first (tcg_products), else set + printed number, else
    set + name. Nothing here creates cards or variants; a line that does not match
    is stored as unmatched.
  * Refund notes ("A partial refund was issued on <date> for the amount of $X",
    with a refundId in the (Details) link) become tcg_order_refunds rows.
  * A file is read once (sha256 recorded in tcg_order_files); a changed file is
    read again. Files still being written (modified in the last 90 seconds) are
    skipped. Files of our own naming older than 7 days that were read are
    deleted (the data lives in the database; the pages can be re-grabbed).
"""

import datetime as dt
import glob
import hashlib
import os
import re
import time
from collections import Counter

from bs4 import BeautifulSoup
from psycopg2.extras import execute_values

from db.connection import db_cursor
from importer.tcgplayer_html import (
    COSMOS_TEXTURE_VARIANTS, FOIL_PATTERNS, REVERSE_HOLO_OVERRIDE_PATTERNS,
    REVERSE_HOLO_PATTERN_SETS, _extract_foil_fields, _extract_foil_type,
    _load_foil_pattern_codes, _resolve_foil_pattern_code,
)
from importer.tcg_messages import export_follow_up, ingest_msglist, ingest_thread, message_file_kind
from importer.tcg_outbox import ingest_msg_result, ingest_rate_result
from utils.set_name_map import _strip_tcgplayer_prefix

DEFAULT_INBOX = os.getenv("TCG_ORDERS_INBOX", r"C:\Users\Fei\Dropbox\tcg_orders\inbox")
SETTLE_SECONDS = 90
KEEP_DAYS = 7

ORDER_RE = re.compile(r"[A-F0-9]{8}-[A-F0-9]{6}-[A-F0-9]{5}")
STATUS_MAP = {
    "shipping not confirmed": "not_shipped",
    "shipped without tracking": "shipped_no_tracking",
    "shipped with tracking": "shipped_tracking",
    "canceled": "cancelled",
    "cancelled": "cancelled",
}
SECTION_TITLES = {"order summary", "ship to", "bill to", "shipped and sold by"}
PAGES_RE = re.compile(r"^orders_(\d{8})_.*page\d+\.html$", re.I)
OK_RE = re.compile(r"^grab_ok_(\d{8})(?:_(\w+))?\.txt$", re.I)
FAILED_RE = re.compile(r"^run_failed_(\d{8}).*\.txt$", re.I)
TRACK_RE = re.compile(r"^tracking_(\d{8})\.csv$", re.I)   # Grok: one line per tracked order, read from its tracking page


def _is_ours(name: str) -> bool:
    return bool(PAGES_RE.match(name) or OK_RE.match(name) or FAILED_RE.match(name) or TRACK_RE.match(name)
                or message_file_kind(name))


def _loose_date(text, near: dt.date):
    """'Monday, November 30' / 'Nov 30, 2026' / '2026-11-30' -> date. A date without a year gets the
    year that puts it closest to `near` (the day the file was made)."""
    t = re.sub(r"\s+", " ", str(text or "").strip())
    if not t:
        return None
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", t)
    if m:
        return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    t = re.sub(r"^[A-Za-z]+day,? ", "", t)
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y"):
        try:
            return dt.datetime.strptime(t, fmt).date()
        except ValueError:
            pass
    for fmt in ("%B %d", "%b %d"):
        try:
            d = dt.datetime.strptime(t, fmt)
        except ValueError:
            continue
        best = None
        for yr in (near.year - 1, near.year, near.year + 1):
            cand = dt.date(yr, d.month, d.day)
            if best is None or abs((cand - near).days) < abs((best - near).days):
                best = cand
        return best
    return None


def ingest_tracking_csv(path: str, grabbed: dt.date, dry_run: bool) -> tuple[int, int]:
    """tracking_<YYYYMMDD>.csv columns: tracking_number, status, estimated_delivery, delivered_on, last_event.
    Fills the carrier_* columns of the matching tcg_orders row. Returns (rows read, orders updated)."""
    import csv
    n = updated = 0
    with open(path, encoding="utf-8-sig", newline="") as fh, db_cursor() as cur:
        for row in csv.DictReader(fh):
            tn = (row.get("tracking_number") or "").strip()
            if not tn:
                continue
            n += 1
            status = (row.get("status") or "").strip() or None
            est = _loose_date(row.get("estimated_delivery"), grabbed)
            delivered = _loose_date(row.get("delivered_on"), grabbed)
            if delivered is None and status and re.search(r"deliver", status, re.I) and not re.search(r"est|out for|attempt|expected|on its way", status, re.I):
                delivered = est
            if dry_run:
                updated += 1
                continue
            cur.execute("""UPDATE tcg_orders SET carrier_status = %s, carrier_est_delivery = %s, carrier_delivered_on = %s,
                               carrier_summary = %s, carrier_error = NULL, carrier_checked_at = now(),
                               carrier = COALESCE(NULLIF(%s, ''), carrier)
                           WHERE tracking_number = %s""",
                        (status, None if delivered else est, delivered, (row.get("last_event") or "").strip() or None,
                         (row.get("carrier") or "").strip().upper(), tn))
            updated += cur.rowcount
    return n, updated


def log(msg: str):
    print(f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


# ── Reading the pages ────────────────────────────────────────────────────────

def _money(s):
    m = re.search(r"\$?\s*([\d,]+\.\d{2})", s or "")
    return float(m.group(1).replace(",", "")) if m else None


def _date(s):
    try:
        return dt.datetime.strptime(re.sub(r"\s+", " ", (s or "").strip()), "%B %d, %Y").date()
    except ValueError:
        return None


def _split_name(raw: str, pattern_codes: dict | None = None):
    """'Eevee - 116/128 (30th Celebration) (Cosmos Holo)' -> ('Eevee', '116', 'Cosmos Holo').
    Trailing parentheses are peeled off: a known finish/pattern becomes the variant, any
    other (a series label such as '(30th Celebration)') is dropped."""
    s = (raw or "").strip()
    groups = []
    while True:
        m = re.search(r"\s*\(([^)]+)\)\s*$", s)
        if not m:
            break
        groups.append(m.group(1).strip())
        s = s[:m.start()].strip()
    known = set(FOIL_PATTERNS) | set(COSMOS_TEXTURE_VARIANTS) | {"reverse holo", "reverse holofoil", "holo", "holofoil"}
    variant = next((g for g in groups if g.lower() in known
                    or (pattern_codes and _resolve_foil_pattern_code(g, pattern_codes))), None)
    number = None
    m = re.search(r"\s*-\s*([A-Za-z]{0,4}\d+[A-Za-z]?(?:/\d+[A-Za-z]*)?)\s*$", s)
    if m:
        number = m.group(1).split("/")[0]
        s = s[:m.start()].strip()
    return s, number, variant


def _carrier(tracking: str | None):
    if not tracking:
        return None
    t = tracking.strip().upper()
    if t.startswith("1Z"):
        return "UPS"
    if t.startswith("9") or t.startswith("42"):
        return "USPS"
    return None


def parse_orders(html: str, pattern_codes: dict) -> list[dict]:
    """Every order block on a list page -> a dict (order, lines, refunds)."""
    soup = BeautifulSoup(html, "html.parser")
    orders = []
    for w in soup.find_all("div", class_="orderWrap"):
        text = w.get_text(" ", strip=True)
        m = ORDER_RE.search(text)
        if not m:
            continue
        o = {"order_number": m.group(0), "lines": [], "refunds": []}

        header = w.find("div", class_="orderHeader")
        for span in (header.find_all("span", recursive=False) if header else []):
            title = span.find("span", class_="orderTitle")
            if not title:
                continue
            label = title.get_text(strip=True).lower()
            value = " ".join(span.get_text(" ", strip=True).split())[len(title.get_text(strip=True)):].strip()
            if label == "order date":
                o["ordered_on"] = _date(value)
            elif label == "channel":
                o["channel"] = value

        for tr in w.select(".orderSummary table tr"):
            tds = tr.find_all("td")
            if len(tds) != 2:
                continue
            label = tds[0].get_text(" ", strip=True).lower()
            val = tds[1].get_text(" ", strip=True)
            if label.startswith("quantity"):
                o["quantity"] = int(re.sub(r"\D", "", val) or 0)
            elif label.startswith("subtotal"):
                o["subtotal"] = _money(val)
            elif label.startswith("shipping"):
                o["shipping"] = _money(val)
            elif label.startswith("sales tax"):
                o["tax"] = _money(val)
            elif label.startswith("total"):
                o["total"] = _money(val)

        # seller + status + tracking + estimated delivery
        content = w.find("div", class_="orderContent") or w
        for title in content.find_all("span", class_="orderTitle"):
            label = title.get_text(" ", strip=True)
            low = label.lower().rstrip(":").strip()
            if low == "shipped and sold by":
                nxt = title.find_next("span")
                a = nxt.find("a") if nxt else None
                o["seller"] = (a.get_text(strip=True) if a else (nxt.get_text(strip=True) if nxt else None))
            elif low not in SECTION_TITLES and low in STATUS_MAP:
                o["raw_status"] = label.rstrip(":").strip()
                o["status"] = STATUS_MAP[low]
                holder = title.parent
                # the tracking link is the first link AFTER this status title (not the seller link earlier in the block)
                a = title.find_next("a", href=True)
                if a and "/track/" in a["href"]:
                    o["tracking_number"] = a.get_text(strip=True)
                    o["tracking_url"] = a["href"]
                    o["carrier"] = _carrier(o["tracking_number"])
                tail = " ".join(holder.get_text(" ", strip=True).split())
                em = re.search(r"([A-Za-z ]+?)\s*\(est\.\s*delivery by ([A-Za-z]+ \d{1,2}, \d{4})\)\s*-\s*\$([\d,.]+)", tail)
                if em:
                    o["ship_method"] = em.group(1).strip()
                    o["est_delivery"] = _date(em.group(2))
        if "status" not in o:
            continue          # not an order block we understand

        # TCGplayer shows "Rate Transaction" only while the order has not been rated (sql/tcg_ratings.sql)
        o["rate_available"] = w.find("input", attrs={"value": "Rate Transaction"}) is not None

        # refunds: <b>A partial refund</b> was issued on <date> for the amount of $X. <a ...refundId=N>(Details)</a>
        for a in w.find_all("a", href=re.compile(r"refundId=(\d+)")):
            rid = int(re.search(r"refundId=(\d+)", a["href"]).group(1))
            b = a.find_previous("b")
            kind = "full" if b and "full" in b.get_text(" ", strip=True).lower() else "partial"
            note = ""
            if b is not None and b.next_sibling is not None:
                note = str(b.next_sibling)
            rm = re.search(r"issued on ([A-Za-z]+ \d{1,2}, \d{4}) for the amount of \$([\d,]+\.\d{2})", " ".join(note.split()))
            if rm:
                o["refunds"].append({"refund_id": rid, "issued_on": _date(rm.group(1)),
                                     "amount": float(rm.group(2).replace(",", "")), "kind": kind})

        # card lines
        line_no = 0
        for tr in w.select("tbody tr"):
            items_td = tr.find("td", class_="orderHistoryItems")
            if not items_td:
                continue
            line_no += 1
            img = items_td.find("img")
            pm = re.search(r"/product/(\d+)_", img["src"]) if img and img.get("src") else None
            pid = int(pm.group(1)) if pm and int(pm.group(1)) > 0 else None
            a = items_td.find("a")
            slug = a["href"] if a and a.get("href") else None
            raw_name = a.get_text(" ", strip=True) if a else ""
            span = items_td.find("span")
            texts = [t.strip() for t in span.stripped_strings] if span else []
            set_label = texts[1] if len(texts) > 1 else None
            detail = [t.strip() for t in tr.find("td", class_="orderHistoryDetail").stripped_strings] if tr.find("td", class_="orderHistoryDetail") else []
            rarity = next((d.split(":", 1)[1].strip() for d in detail if d.lower().startswith("rarity:")), None)
            cond = next((d.split(":", 1)[1].strip() for d in detail if d.lower().startswith("condition:")), "")
            price = _money(tr.find("td", class_="orderHistoryPrice").get_text(" ", strip=True)) if tr.find("td", class_="orderHistoryPrice") else None
            qty_td = tr.find("td", class_="orderHistoryQuantity")
            qty = int(re.sub(r"\D", "", qty_td.get_text()) or 1) if qty_td else 1

            name, number, variant = _split_name(raw_name, pattern_codes)
            low = cond.lower()
            printing = "Reverse Holofoil" if "reverse holofoil" in low else ("Holofoil" if "holofoil" in low else "Normal")
            foil_type, foil_pattern = _extract_foil_fields(variant, _extract_foil_type(cond))
            texture = None
            if (foil_pattern or "").strip().lower() in COSMOS_TEXTURE_VARIANTS:
                texture, foil_pattern = "cosmos", None
            code = _resolve_foil_pattern_code(foil_pattern, pattern_codes)
            if code:
                foil_pattern = code
            if _strip_tcgplayer_prefix(set_label or "") in REVERSE_HOLO_PATTERN_SETS and foil_pattern in REVERSE_HOLO_OVERRIDE_PATTERNS:
                foil_type = "reverse_holo"
            o["lines"].append({
                "line_no": line_no, "product_id": pid, "product_slug": slug, "card_name": name,
                "card_number": number, "set_label": set_label, "rarity": rarity, "condition_raw": cond,
                "printing": printing, "foil_type": foil_type, "foil_pattern": foil_pattern, "texture": texture,
                "unit_price": price, "quantity": qty,
            })
        orders.append(o)
    return orders


# ── Matching lines to our cards / variants (in memory) ──────────────────────

def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


class Matcher:
    def __init__(self):
        with db_cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '60s'")
            cur.execute("SELECT tcgplayer_label, set_id::text AS set_id, api_set_id FROM tcgplayer_set_aliases")
            self.aliases = {r["tcgplayer_label"]: dict(r) for r in cur.fetchall()}
            cur.execute("SELECT id::text AS id, set_code FROM card_sets WHERE set_code IS NOT NULL")
            self.set_by_code = {r["set_code"]: r["id"] for r in cur.fetchall()}
            cur.execute("SELECT id::text AS id, name, series FROM card_sets")
            self.set_by_name = {r["name"].lower(): r["id"] for r in cur.fetchall()}
            cur.execute("SELECT id::text AS id, set_id::text AS set_id, card_number, name, rarity FROM card_master")
            self.cards = {}
            self.by_number, self.by_name, self.by_global = {}, {}, {}
            for r in cur.fetchall():
                self.cards[r["id"]] = r
                num = (r["card_number"] or "").lower()
                self.by_number.setdefault((r["set_id"], num), []).append(r)
                self.by_name.setdefault((r["set_id"], (r["name"] or "").lower()), []).append(r)
                self.by_global.setdefault(((r["name"] or "").lower(), num), []).append(r)
            cur.execute("""SELECT id::text AS id, card_id::text AS card_id, foil_type, foil_pattern, texture, material, size,
                                  stamp_type, source_type FROM card_variants""")
            self.variants = {}
            self.variant_card = {}
            for r in cur.fetchall():
                self.variants.setdefault(r["card_id"], []).append(r)
                self.variant_card[r["id"]] = r["card_id"]
            cur.execute("SELECT product_id, printing, variant_id::text AS variant_id FROM tcg_products")
            self.product_map = {(r["product_id"], r["printing"]): r["variant_id"] for r in cur.fetchall()}
        self.learned = {}

    def _set_id(self, set_label):
        if not set_label:
            return None
        cleaned = _strip_tcgplayer_prefix(set_label)
        alias = self.aliases.get(cleaned)
        if not alias:
            cleaned2 = re.sub(r"^[A-Z][^:]+:\s*", "", cleaned).strip()
            alias = self.aliases.get(cleaned2) if cleaned2 != cleaned else None
        if not alias:
            # no alias row: a set whose own name is the label (e.g. "Delta Reign")
            return self.set_by_name.get(cleaned.lower())
        if alias.get("set_id"):
            return alias["set_id"]
        return self.set_by_code.get(alias.get("api_set_id"))

    def _variant(self, card_id, line):
        for v in self.variants.get(card_id, []):
            if (v["foil_type"] == line["foil_type"] and v["foil_pattern"] == line["foil_pattern"]
                    and v["texture"] == line["texture"] and not v["material"] and not v["size"]
                    and not v["stamp_type"] and not v["source_type"]):
                return v["id"]
        return None

    def resolve(self, line):
        """-> (card_id, variant_id, status, source)"""
        pid, printing = line.get("product_id"), line["printing"]
        if pid and (pid, printing) in self.product_map:
            vid = self.product_map[(pid, printing)]
            return self.variant_card.get(vid), vid, "matched", "product_map"
        set_id = self._set_id(line.get("set_label"))
        card, source = None, None
        number = line.get("card_number")
        nums = list(dict.fromkeys([number, number.lstrip("0") or number])) if number else []
        if set_id:
            for n in nums:
                hits = self.by_number.get((set_id, n.lower()), [])
                if len(hits) == 1 and _norm(hits[0]["name"]) == _norm(line["card_name"]):
                    card, source = hits[0], "set_number"
                    break
            if card is None:
                hits = self.by_name.get((set_id, line["card_name"].lower()), [])
                if len(hits) > 1 and line.get("rarity"):
                    hits = [h for h in hits if (h["rarity"] or "").lower() == line["rarity"].lower()]
                if len(hits) == 1:
                    card, source = hits[0], "set_name"
        if card is None and nums:
            # set label did not help (e.g. "Miscellaneous Cards & Products", promos): same name + number anywhere
            for n in nums:
                hits = self.by_global.get((line["card_name"].lower(), n.lower()), [])
                if len(hits) == 1:
                    card, source = hits[0], "name_number"
                    break
        if card is None:
            return None, None, "unmatched", None
        vid = self._variant(card["id"], line)
        if vid is None:
            return card["id"], None, "card_only", source
        if pid and source == "set_number":
            self.learned[(pid, printing)] = (vid, card["id"], line)
        return card["id"], vid, "matched", source


# ── Writing ──────────────────────────────────────────────────────────────────

def _known_order_numbers(cur) -> set:
    cur.execute("SELECT DISTINCT upper(reference_id) AS r FROM purchases WHERE reference_id IS NOT NULL")
    known = {r["r"] for r in cur.fetchall()}
    cur.execute("SELECT DISTINCT upper(order_number) AS r FROM staging WHERE order_number IS NOT NULL")
    return known | {r["r"] for r in cur.fetchall()}


def upsert_orders(orders: list[dict], source_file: str, matcher: Matcher, dry_run: bool) -> Counter:
    stats = Counter()
    with db_cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '120s'")
        known = _known_order_numbers(cur)
        cur.execute("SELECT order_number, status, tracking_number, est_delivery, total, quantity, received_at FROM tcg_orders")
        existing = {r["order_number"]: dict(r) for r in cur.fetchall()}
        cur.execute("SELECT DISTINCT order_number FROM tcg_order_lines")
        has_lines = {r["order_number"] for r in cur.fetchall()}
        cur.execute("SELECT order_number, refund_id FROM tcg_order_refunds")
        have_refunds = {(r["order_number"], r["refund_id"]) for r in cur.fetchall()}

        for o in orders:
            num = o["order_number"]
            refund_total = round(sum(r["amount"] for r in o["refunds"]), 2)
            in_system = num.upper() in known
            old = existing.get(num)
            if old is None:
                stats["new"] += 1
            changed = (old is None or old["status"] != o["status"] or old["tracking_number"] != o.get("tracking_number")
                       or old["est_delivery"] != o.get("est_delivery") or float(old["total"] or 0) != float(o.get("total") or 0)
                       or (old["quantity"] or 0) != (o.get("quantity") or 0))
            stats[o["status"]] += 1
            if in_system:
                stats["in_system"] += 1
            if dry_run:
                stats["lines"] += len(o["lines"])
                stats["refunds"] += len(o["refunds"])
                continue
            cur.execute("""
                INSERT INTO tcg_orders (order_number, ordered_on, channel, seller, raw_status, status, tracking_number, tracking_url,
                                        carrier, ship_method, est_delivery, quantity, subtotal, shipping, tax, total, refund_total,
                                        received_at, received_by, source_file, rate_available, rate_seen_at)
                VALUES (%(order_number)s, %(ordered_on)s, %(channel)s, %(seller)s, %(raw_status)s, %(status)s, %(tracking_number)s,
                        %(tracking_url)s, %(carrier)s, %(ship_method)s, %(est_delivery)s, %(quantity)s, %(subtotal)s, %(shipping)s,
                        %(tax)s, %(total)s, %(refund_total)s,
                        CASE WHEN %(in_system)s THEN now() END, CASE WHEN %(in_system)s THEN 'auto_system' END, %(source_file)s,
                        %(rate_available)s, now())
                ON CONFLICT (order_number) DO UPDATE SET
                    seller = EXCLUDED.seller, raw_status = EXCLUDED.raw_status, status = EXCLUDED.status,
                    tracking_number = COALESCE(EXCLUDED.tracking_number, tcg_orders.tracking_number),
                    tracking_url = COALESCE(EXCLUDED.tracking_url, tcg_orders.tracking_url),
                    carrier = COALESCE(EXCLUDED.carrier, tcg_orders.carrier),
                    ship_method = EXCLUDED.ship_method, est_delivery = EXCLUDED.est_delivery, quantity = EXCLUDED.quantity,
                    subtotal = EXCLUDED.subtotal, shipping = EXCLUDED.shipping, tax = EXCLUDED.tax, total = EXCLUDED.total,
                    refund_total = EXCLUDED.refund_total, source_file = EXCLUDED.source_file, last_seen_at = now(),
                    rate_available = EXCLUDED.rate_available, rate_seen_at = now(),
                    last_changed_at = CASE WHEN %(changed)s OR tcg_orders.refund_total IS DISTINCT FROM EXCLUDED.refund_total
                                           THEN now() ELSE tcg_orders.last_changed_at END,
                    received_at = COALESCE(tcg_orders.received_at, CASE WHEN %(in_system)s THEN now() END),
                    received_by = COALESCE(tcg_orders.received_by, CASE WHEN %(in_system)s THEN 'auto_system' END)
            """, {**{k: o.get(k) for k in ("order_number", "ordered_on", "channel", "seller", "raw_status", "status", "tracking_number",
                                           "tracking_url", "carrier", "ship_method", "est_delivery", "quantity", "subtotal",
                                           "shipping", "tax", "total", "rate_available")},
                  "refund_total": refund_total, "in_system": in_system, "changed": changed, "source_file": source_file})
            for r in o["refunds"]:
                if (num, r["refund_id"]) not in have_refunds:
                    cur.execute("""INSERT INTO tcg_order_refunds (order_number, refund_id, issued_on, amount, kind)
                                   VALUES (%s, %s, %s, %s, %s) ON CONFLICT (order_number, refund_id) DO NOTHING""",
                                (num, r["refund_id"], r["issued_on"], r["amount"], r["kind"]))
                    stats["refunds"] += 1
            if num not in has_lines:
                rows = []
                for ln in o["lines"]:
                    card_id, variant_id, status, source = matcher.resolve(ln)
                    stats["lines_" + status] += 1
                    rows.append((num, ln["line_no"], ln["product_id"], ln["product_slug"], ln["card_name"], ln["card_number"],
                                 ln["set_label"], ln["rarity"], ln["condition_raw"], ln["printing"], ln["foil_type"],
                                 ln["foil_pattern"], ln["texture"], ln["unit_price"], ln["quantity"], card_id, variant_id,
                                 status, source))
                if rows:
                    execute_values(cur, """INSERT INTO tcg_order_lines (order_number, line_no, product_id, product_slug, card_name,
                        card_number, set_label, rarity, condition_raw, printing, foil_type, foil_pattern, texture, unit_price,
                        quantity, card_id, variant_id, match_status, match_source) VALUES %s
                        ON CONFLICT (order_number, line_no) DO NOTHING""", rows)
                has_lines.add(num)

        if not dry_run and matcher.learned:
            rows = [(pid, printing, vid, cid, ln["card_name"], ln["set_label"], ln["product_slug"])
                    for (pid, printing), (vid, cid, ln) in matcher.learned.items() if (pid, printing) not in matcher.product_map]
            if rows:
                execute_values(cur, """INSERT INTO tcg_products (product_id, printing, variant_id, card_id, name, set_label, slug)
                                       VALUES %s ON CONFLICT (product_id, printing) DO NOTHING""", rows)
                for pid, printing, vid, *_ in rows:
                    matcher.product_map[(pid, printing)] = vid
                stats["products_learned"] += len(rows)
            matcher.learned.clear()
    return stats


def rematch_unmatched(matcher: "Matcher | None" = None) -> int:
    """Try again on every stored line that is not matched to an exact variant yet, using the
    current set aliases / learned products. Lines you linked by hand (match_source 'manual')
    are never touched. Returns how many lines improved."""
    matcher = matcher or Matcher()
    improved = 0
    with db_cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '120s'")
        cur.execute("""SELECT id::text AS id, product_id, printing, set_label, card_name, card_number, rarity, foil_type,
                              foil_pattern, texture, match_status FROM tcg_order_lines
                       WHERE match_status <> 'matched' AND COALESCE(match_source, '') <> 'manual'""")
        rows = cur.fetchall()
        rank = {"unmatched": 0, "card_only": 1, "matched": 2}
        for r in rows:
            card_id, variant_id, status, source = matcher.resolve(dict(r))
            if rank[status] > rank[r["match_status"]]:
                cur.execute("""UPDATE tcg_order_lines SET card_id = %s, variant_id = %s, match_status = %s, match_source = %s
                               WHERE id = %s""", (card_id, variant_id, status, source, r["id"]))
                improved += 1
    return improved


def _sha(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def process_inbox(inbox: str = DEFAULT_INBOX, dry_run: bool = False, keep_days: int = KEEP_DAYS) -> dict:
    """Read every new or changed file in the inbox, oldest first. Returns a summary."""
    if not os.path.isdir(inbox):
        log(f"inbox not found: {inbox}")
        return {"error": "inbox not found"}
    files = sorted((f for f in glob.glob(os.path.join(inbox, "*")) if os.path.isfile(f)), key=os.path.getmtime)
    with db_cursor() as cur:
        cur.execute("SELECT file_name, sha256 FROM tcg_order_files")
        done = {r["file_name"]: r["sha256"] for r in cur.fetchall()}
    todo = []
    for f in files:
        name = os.path.basename(f)
        if not _is_ours(name):
            continue
        if time.time() - os.path.getmtime(f) < SETTLE_SECONDS:
            log(f"  {name}: still being written, skipped for now")
            continue
        sha = _sha(f)
        if done.get(name) == sha:
            continue
        todo.append((f, name, sha))
    summary = {"files": len(todo), "orders": 0, "stats": Counter(), "kinds": Counter(), "new_messages": 0, "followups": None}
    if todo:
        log(f"{len(todo)} new/changed file(s) in the inbox" + (" (dry run, nothing written)" if dry_run else ""))
    matcher = None
    pattern_codes = None
    for f, name, sha in todo:
        mk = message_file_kind(name)      # message-center pages from Grok (tcg_messages.py)
        if mk:
            pm = okm = fm = tm = None
            grabbed = mk[1] or dt.date.today()
        else:
            pm, okm, fm, tm = PAGES_RE.match(name), OK_RE.match(name), FAILED_RE.match(name), TRACK_RE.match(name)
            grabbed = dt.datetime.strptime((pm or okm or fm or tm).group(1), "%Y%m%d").date()
        kind, seen, saved, note, window = None, None, None, None, None
        if mk:
            kind = mk[0]
            if kind == "msglist":
                st = ingest_msglist(f, dry_run)
                seen, saved = st["threads_on_page"], st["tracked"]
                note = f"{st['threads_on_page']} threads on the page, {st['tracked']} on tracked orders, {st['unread']} unread"
            elif kind == "thread":
                st = ingest_thread(f, dry_run)
                seen, saved = st.get("messages"), st.get("new")
                summary["new_messages"] += st.get("new") or 0
                note = st.get("error") or (f"thread {st['thread_id']} order {st['order']}: {st['messages']} messages, {st['new']} new"
                                           + ("" if st["tracked"] else " (order not tracked, skipped)"))
            elif kind == "rate_result":
                st = ingest_rate_result(f, dry_run)
                note = st.get("error") or (f"rating {st['id']}: {st['status']}" + ("" if st["found"] else " (no matching rating, ignored)"))
                summary["results"] = summary.get("results", 0) + 1
            elif kind == "msg_result":
                st = ingest_msg_result(f, dry_run)
                note = st.get("error") or (f"message {st['id']}: {st['status']}" + ("" if st["found"] else " (no matching message, ignored)"))
                summary["results"] = summary.get("results", 0) + 1
            else:
                text = open(f, encoding="utf-8", errors="replace").read()
                note = text[:300].replace("\n", " ")
                if kind == "msg_failed":
                    log(f"  GROK REPORTED A FAILED MESSAGE RUN ({name}): {note}")
            log(f"  {name}: {note}")
        elif pm:
            kind = "pages"
            if matcher is None:
                matcher, pattern_codes = Matcher(), _load_foil_pattern_codes()
            with open(f, encoding="utf-8", errors="replace") as fh:
                orders = parse_orders(fh.read(), pattern_codes)
            st = upsert_orders(orders, name, matcher, dry_run)
            summary["orders"] += len(orders)
            summary["stats"].update(st)
            note = f"{len(orders)} orders"
            log(f"  {name}: {len(orders)} orders ({st.get('new', 0)} new)")
        elif tm:
            kind = "tracking"
            seen, saved = ingest_tracking_csv(f, grabbed, dry_run)
            note = f"{seen} tracking rows, {saved} orders updated"
            log(f"  {name}: {note}")
        else:
            text = open(f, encoding="utf-8", errors="replace").read()
            if okm:
                kind = "grab_ok"
                window = okm.group(2)
                s = re.search(r"orders seen:\s*(\d+)", text, re.I)
                v = re.search(r"orders saved:\s*(\d+)", text, re.I)
                seen, saved = (int(s.group(1)) if s else None), (int(v.group(1)) if v else None)
                note = text[:200].replace("\n", " ")
            else:
                kind = "run_failed"
                note = text[:300].replace("\n", " ")
                log(f"  GROK REPORTED A FAILED RUN ({name}): {note}")
        summary["kinds"][kind] += 1
        if not dry_run:
            with db_cursor() as cur:
                cur.execute("""INSERT INTO tcg_order_files (file_name, sha256, kind, grabbed_on, window_days, orders_seen, orders_saved, note)
                               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                               ON CONFLICT (file_name) DO UPDATE SET sha256 = EXCLUDED.sha256, kind = EXCLUDED.kind,
                                   orders_seen = EXCLUDED.orders_seen, orders_saved = EXCLUDED.orders_saved, note = EXCLUDED.note,
                                   parsed_at = now()""", (name, sha, kind, grabbed, window, seen, saved, note))
    if not dry_run:
        try:
            n_follow = export_follow_up()
            summary["followups"] = n_follow
            log(f"  follow_up.txt: {n_follow} order(s) to follow up")
        except Exception as e:      # the outbox folder may be missing/locked; never block the order import
            log(f"  could not write follow_up.txt: {e}")
        n = rematch_unmatched(matcher)
        if n:
            log(f"  re-matched {n} previously unmatched card line(s)")
    # clean-up: our own files, read already, older than keep_days
    if not dry_run:
        with db_cursor() as cur:
            cur.execute("SELECT file_name, sha256 FROM tcg_order_files")
            done = {r["file_name"]: r["sha256"] for r in cur.fetchall()}
        cutoff = time.time() - keep_days * 86400
        removed = 0
        for f in files:
            name = os.path.basename(f)
            if _is_ours(name) and os.path.getmtime(f) < cutoff \
                    and done.get(name) == _sha(f):
                try:
                    os.remove(f)
                    removed += 1
                except OSError as e:
                    log(f"  could not delete {name}: {e}")
        if removed:
            log(f"  removed {removed} file(s) older than {keep_days} days")
    return summary


def status_report():
    with db_cursor() as cur:
        cur.execute("""SELECT max(parsed_at) FILTER (WHERE kind = 'pages') AS last_pages,
                              max(grabbed_on) FILTER (WHERE kind = 'grab_ok') AS last_ok,
                              max(grabbed_on) FILTER (WHERE kind = 'run_failed') AS last_failed
                       FROM tcg_order_files""")
        r = cur.fetchone()
        print(f"last pulled: pages read {r['last_pages']}, Grok success note for {r['last_ok']}, last failure note {r['last_failed']}")
        cur.execute("SELECT status, (received_at IS NOT NULL) AS received, count(*) AS n FROM tcg_orders GROUP BY 1, 2 ORDER BY 1, 2")
        print("orders by status / received:")
        for r in cur.fetchall():
            print(f"  {r['status']:<22} received={str(r['received']):<5} {r['n']}")
        cur.execute("""SELECT order_number, ordered_on, seller, status, quantity, total, est_delivery, refund_total FROM tcg_orders
                       WHERE received_at IS NULL AND status <> 'cancelled' ORDER BY ordered_on""")
        rows = cur.fetchall()
        print(f"open (not received, not cancelled): {len(rows)}")
        for r in rows:
            print(f"  {r['order_number']} {r['ordered_on']} {str(r['seller'])[:20]:<20} {r['status']:<20} qty {r['quantity']:>3} ${r['total']}"
                  f" est {r['est_delivery']}" + (f" refund ${r['refund_total']}" if r["refund_total"] else ""))
        cur.execute("SELECT match_status, count(*) AS n FROM tcg_order_lines GROUP BY 1 ORDER BY 1")
        print("lines:", {r["match_status"]: r["n"] for r in cur.fetchall()})
