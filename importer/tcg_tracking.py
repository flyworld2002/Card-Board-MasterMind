"""
importer/tcg_tracking.py
Asks the carrier (USPS, UPS) about every open TCGplayer order that has a tracking number and stores
the carrier's estimated delivery date (or the actual delivery date) on tcg_orders (carrier_* columns,
sql/tcg_tracking.sql). The Incoming page shows it next to TCGplayer's own estimate.

  python main.py --tcg-tracking-refresh [--dry-run]     # also runs inside the daily --tcg-orders-process

Credentials (.env, both free from the carriers' developer sites; a carrier without credentials is skipped):
  USPS_CLIENT_ID, USPS_CLIENT_SECRET   developers.usps.com -> app -> Consumer Key / Consumer Secret
  UPS_CLIENT_ID,  UPS_CLIENT_SECRET    developer.ups.com    -> app -> Client ID / Client Secret (Tracking product)

OPTIONAL: the normal way dates arrive is the tracking pages read by Grok or by Claude in Chrome
(tracking_<YYYYMMDD>.csv in the Dropbox inbox, see tcg_orders.py). This API route only fills the same columns.
Delivery information is stored only; nothing here marks an order received. A tracking number that
is not found is retried on later days (carriers often don't know a label for a day or two).
"""

import base64
import datetime as dt
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from db.connection import db_cursor
from importer.tcg_orders import log

TIMEOUT = 30
_tokens: dict = {}


def _http(method: str, url: str, headers: dict | None = None, data: bytes | None = None) -> dict:
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.load(r)


def _date(v) -> dt.date | None:
    """'2026-12-03T20:00:00Z' / '2026-12-03' / '20261203' / 'December 3, 2026' -> date."""
    if not v:
        return None
    s = str(v).strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s) or re.match(r"^(\d{4})(\d{2})(\d{2})$", s)
    if m:
        try:
            return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def _walk(obj):
    """Every (key, value) pair anywhere in a JSON document."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k, v
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


# ── USPS (Tracking API v3, OAuth client credentials) ────────────────────────

def _usps_token() -> str | None:
    cid, sec = os.getenv("USPS_CLIENT_ID"), os.getenv("USPS_CLIENT_SECRET")
    if not (cid and sec):
        return None
    if _tokens.get("usps", (0, ""))[0] > time.time() + 60:
        return _tokens["usps"][1]
    r = _http("POST", "https://apis.usps.com/oauth2/v3/token", {"Content-Type": "application/json"},
              json.dumps({"grant_type": "client_credentials", "client_id": cid, "client_secret": sec}).encode())
    _tokens["usps"] = (time.time() + int(r.get("expires_in", 1800)), r["access_token"])
    return r["access_token"]


def lookup_usps(number: str) -> dict:
    token = _usps_token()
    if token is None:
        return {"error": "USPS credentials not set"}
    try:
        d = _http("GET", f"https://apis.usps.com/tracking/v3/tracking/{urllib.parse.quote(number)}?expand=DETAIL",
                  {"Authorization": f"Bearer {token}", "Accept": "application/json"})
    except urllib.error.HTTPError as e:
        if e.code in (400, 404):
            return {"error": "USPS has no record of this number yet"}
        return {"error": f"USPS lookup failed ({e.code})"}
    status = d.get("statusCategory") or d.get("status") or ""
    est = None
    for k, v in _walk(d):
        if re.search(r"(expected|predicted|scheduled|guaranteed).*deliver", k, re.I) and _date(v):
            est = _date(v)
            break
    delivered = None
    if "deliver" in status.lower() and "out for" not in status.lower() and "attempt" not in status.lower():
        for ev in d.get("trackingEvents") or []:
            if "delivered" in str(ev.get("eventType", "")).lower():
                delivered = _date(ev.get("eventTimestamp"))
                break
        delivered = delivered or next((_date(v) for k, v in _walk(d) if re.fullmatch(r"(?i)delivery_?date", k) and _date(v)), None)
    return {"status": status or None, "est": None if delivered else est, "delivered": delivered,
            "summary": d.get("statusSummary") or d.get("status")}


# ── UPS (Track API v1, OAuth client credentials) ────────────────────────────

def _ups_token() -> str | None:
    cid, sec = os.getenv("UPS_CLIENT_ID"), os.getenv("UPS_CLIENT_SECRET")
    if not (cid and sec):
        return None
    if _tokens.get("ups", (0, ""))[0] > time.time() + 60:
        return _tokens["ups"][1]
    basic = base64.b64encode(f"{cid}:{sec}".encode()).decode()
    r = _http("POST", "https://onlinetools.ups.com/security/v1/oauth/token",
              {"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
              b"grant_type=client_credentials")
    _tokens["ups"] = (time.time() + int(r.get("expires_in", 14000)), r["access_token"])
    return r["access_token"]


def lookup_ups(number: str) -> dict:
    token = _ups_token()
    if token is None:
        return {"error": "UPS credentials not set"}
    try:
        d = _http("GET", f"https://onlinetools.ups.com/api/track/v1/details/{urllib.parse.quote(number)}?locale=en_US",
                  {"Authorization": f"Bearer {token}", "transId": f"cbm-{int(time.time())}", "transactionSrc": "cbm"})
    except urllib.error.HTTPError as e:
        if e.code in (400, 404):
            return {"error": "UPS has no record of this number yet"}
        return {"error": f"UPS lookup failed ({e.code})"}
    try:
        pkg = d["trackResponse"]["shipment"][0]["package"][0]
    except (KeyError, IndexError):
        return {"error": "UPS has no record of this number yet"}
    cur = pkg.get("currentStatus") or {}
    dates = {x.get("type"): _date(x.get("date")) for x in pkg.get("deliveryDate") or []}
    delivered = dates.get("DEL")
    est = dates.get("RDD") or dates.get("SDD")
    desc = cur.get("description") or ""
    return {"status": "Delivered" if delivered else (desc or None), "est": None if delivered else est,
            "delivered": delivered, "summary": desc or None}


def _guess_carrier(number: str, carrier: str | None) -> str:
    if carrier in ("USPS", "UPS"):
        return carrier
    return "UPS" if number.upper().startswith("1Z") else "USPS"


def refresh_tracking(dry_run: bool = False) -> dict:
    with db_cursor() as cur:
        cur.execute("""SELECT order_number, tracking_number, carrier FROM tcg_orders
                       WHERE tracking_number IS NOT NULL AND received_at IS NULL AND status <> 'cancelled'
                         AND (carrier_delivered_on IS NULL)
                       ORDER BY ordered_on""")
        orders = [dict(r) for r in cur.fetchall()]
    stats = {"checked": 0, "delivered": 0, "estimated": 0, "errors": 0, "no_credentials": set()}
    for o in orders:
        carrier = _guess_carrier(o["tracking_number"], o["carrier"])
        try:
            res = (lookup_ups if carrier == "UPS" else lookup_usps)(o["tracking_number"])
        except Exception as e:      # network / unexpected format: note it, carry on with the others
            res = {"error": f"{carrier} lookup failed: {e}"}
        if res.get("error"):
            stats["errors"] += 1
            if "credentials not set" in res["error"]:
                stats["no_credentials"].add(carrier)
        else:
            stats["checked"] += 1
            stats["delivered"] += 1 if res.get("delivered") else 0
            stats["estimated"] += 1 if res.get("est") else 0
        if dry_run:
            print(o["order_number"], carrier, res)
            continue
        if res.get("error"):
            # keep whatever dates are already stored (e.g. read from the tracking page); just note the problem
            with db_cursor() as cur:
                cur.execute("UPDATE tcg_orders SET carrier_error = %s WHERE order_number = %s", (res["error"], o["order_number"]))
            continue
        with db_cursor() as cur:
            cur.execute("""UPDATE tcg_orders SET carrier = COALESCE(carrier, %s), carrier_status = %s, carrier_est_delivery = %s,
                               carrier_delivered_on = %s, carrier_summary = %s, carrier_error = %s, carrier_checked_at = now()
                           WHERE order_number = %s""",
                        (carrier, res.get("status"), res.get("est"), res.get("delivered"), res.get("summary"),
                         res.get("error"), o["order_number"]))
        time.sleep(0.2)
    stats["no_credentials"] = sorted(stats["no_credentials"])
    log(f"tracking: {len(orders)} open tracked orders; {stats['checked']} answered "
        f"({stats['estimated']} with an estimate, {stats['delivered']} delivered), {stats['errors']} without an answer"
        + (f"; no credentials for {', '.join(stats['no_credentials'])}" if stats["no_credentials"] else ""))
    return stats
