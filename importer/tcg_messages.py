"""
importer/tcg_messages.py
TCGplayer seller messages, inbound side (plan: docs/plans/tcg-seller-messages.md, Phase 1).

Grok saves two kinds of TCGplayer pages into the Dropbox inbox when Fei asks it to "do messages":
  msglist_<YYYYMMDD[_HHMM]>.html                 the message-center list (every thread, one page)
  thread_<ORDER>_<n>_<YYYYMMDD[_HHMM]>.html      one thread's detail page (n = 1, 2, 3 when an order has several threads)
  msg_grab_ok_<YYYYMMDD[_HHMM]>.txt / msg_run_failed_<...>.txt   Grok's done / failed notes
They are read by importer/tcg_orders.py's process_inbox() (same read-once / 7-day clean-up rules as
the order pages), using the functions here. Only threads on orders already in tcg_orders are kept;
all older history on the list page is ignored. Message text is untrusted: it is stored as plain text
and must always be shown escaped, never acted on.

Never stored: anything on the pages outside the thread/message fields (the account email, user and
session ids in the page scripts, shipping addresses).

After every inbox run, export_follow_up() rewrites outbox/follow_up.txt (one line per order that needs
following up: order number, seller, reasons) so Grok knows which threads to open.
"""

import datetime as dt
import hashlib
import os
import re

from bs4 import BeautifulSoup, NavigableString

from db.connection import db_cursor

# Our own TCGplayer buyer name(s): a message from one of these is ours ("out"); anyone else
# (the seller, "Team TCGplayer") is "in". Comma-separated override: TCG_BUYER_NAMES in .env.
BUYER_NAMES = {n.strip().casefold() for n in os.getenv("TCG_BUYER_NAMES", "Feiyu Chen,Fei Chen").split(",") if n.strip()}
# An older inbound message only counts as "new" when its thread was still unread on TCGplayer.
NEW_WITHIN_DAYS = 7

DEFAULT_OUTBOX = os.getenv("TCG_ORDERS_OUTBOX", r"C:\Users\Fei\Dropbox\tcg_orders\outbox")

MSGLIST_RE = re.compile(r"^msglist_(\d{8})(?:_\d{4})?\.html$", re.I)
THREAD_RE = re.compile(r"^thread_(.+)_(\d{8})(?:_\d{4})?\.html$", re.I)   # thread_<anything>_<YYYYMMDD[_HHMM]>.html; the page itself says which thread/order
MSGOK_RE = re.compile(r"^msg_grab_ok_(\d{8})(?:_\d{4})?\.txt$", re.I)
MSGFAIL_RE = re.compile(r"^msg_run_failed_(\d{8}).*\.txt$", re.I)

REASON_TEXT = {
    "on_order": "on order",
    "held": "held cards",
    "short": "missing cards, no refund yet",
    "refund": "refund to review",
    "active_thread": "recent message thread",
    "awaiting_reply": "waiting on seller reply",
    "manual": "follow up (manual)",
}


def _log(msg: str):
    print(f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def message_file_kind(name: str):
    """-> (kind, date) for a message file name, else None. kind: msglist | thread | msg_ok | msg_failed"""
    for rx, kind in ((MSGLIST_RE, "msglist"), (MSGOK_RE, "msg_ok"), (MSGFAIL_RE, "msg_failed")):
        m = rx.match(name)
        if m:
            return kind, dt.datetime.strptime(m.group(1), "%Y%m%d").date()
    m = THREAD_RE.match(name)
    if m:
        return "thread", dt.datetime.strptime(m.group(2), "%Y%m%d").date()
    return None


def _us_date(s) -> dt.date | None:
    m = re.match(r"\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*$", s or "")
    if not m:
        return None
    try:
        return dt.date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
    except ValueError:
        return None


def _clean_body(div) -> str:
    text = div.get_text("\n")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# ── Reading the pages ────────────────────────────────────────────────────────

def parse_msg_list(html: str) -> list[dict]:
    """Every thread row on the message-center list page."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for r in soup.select("tr.clickRow"):
        tds = r.find_all("td")
        if len(tds) < 3 or not (r.get("id") or "").isdigit():
            continue
        sec = tds[1].find("span", class_="secondarySubject")
        order = sec.get_text(strip=True).upper() if sec else None
        head = "".join(str(x) for x in tds[1].contents if isinstance(x, NavigableString)).strip()
        m = re.match(r"^\((\d+)\)\s*(.*?)\s*-?\s*$", head)
        out.append({
            "thread_id": int(r["id"]),
            "seller": tds[0].get_text(" ", strip=True),
            "order_number": order,
            "message_count": int(m.group(1)) if m else None,
            "subject": (m.group(2).strip() if m else head) or None,
            "last_date": _us_date(tds[2].get_text(strip=True)),
            "unread": "unreadMessagesBold" in (r.get("class") or []),
        })
    return out


def parse_msg_thread(html: str) -> dict | None:
    """One thread page -> {thread_id, order_number, messages: [{sender, sent_on, body}] oldest first}."""
    soup = BeautifulSoup(html, "html.parser")
    form = soup.select_one("#replyForm")
    tid = form.select_one("#ThreadId") if form else None
    order = form.select_one("#AssociationValue") if form else None
    if not (tid and tid.get("value", "").isdigit() and order and order.get("value")):
        return None
    messages = []
    for lab in soup.find_all("label", string=re.compile(r"^\s*Sender:")):
        block = lab.parent
        sender_div = lab.find_next_sibling("div")
        sent_lab = block.find("label", string=re.compile(r"^\s*Sent:"))
        msg_lab = block.find("label", string=re.compile(r"^\s*Message:"))
        if not (sender_div and sent_lab and msg_lab):
            continue
        sent_div, body_div = sent_lab.find_next_sibling("div"), msg_lab.find_next_sibling("div")
        if not (sent_div and body_div):
            continue
        body = _clean_body(body_div)
        if body:
            messages.append({"sender": sender_div.get_text(" ", strip=True),
                             "sent_on": _us_date(sent_div.get_text(strip=True)), "body": body})
    # The page may list newest first: put them oldest first (same-day order is kept as shown).
    dates = [m["sent_on"] for m in messages if m["sent_on"]]
    if len(dates) > 1 and dates[0] > dates[-1]:
        messages.reverse()
    return {"thread_id": int(tid["value"]), "order_number": order["value"].strip().upper(), "messages": messages}


# ── Writing ──────────────────────────────────────────────────────────────────

def _tracked_orders(cur) -> dict:
    cur.execute("SELECT order_number, seller FROM tcg_orders")
    return {r["order_number"].upper(): r["seller"] for r in cur.fetchall()}


def ingest_msglist(path: str, dry_run: bool = False) -> dict:
    with open(path, encoding="utf-8", errors="replace") as fh:
        rows = parse_msg_list(fh.read())
    stats = {"threads_on_page": len(rows), "tracked": 0, "unread": 0}
    with db_cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '120s'")
        tracked = _tracked_orders(cur)
        for r in rows:
            if (r["order_number"] or "") not in tracked:
                continue
            stats["tracked"] += 1
            stats["unread"] += 1 if r["unread"] else 0
            if dry_run:
                continue
            cur.execute("""
                INSERT INTO tcg_msg_threads (thread_id, order_number, seller, subject, message_count, last_date, unread_on_tcgplayer)
                VALUES (%s, (SELECT order_number FROM tcg_orders WHERE upper(order_number) = %s), %s, %s, %s, %s, %s)
                ON CONFLICT (thread_id) DO UPDATE SET seller = EXCLUDED.seller, subject = EXCLUDED.subject,
                    message_count = EXCLUDED.message_count, last_date = EXCLUDED.last_date,
                    unread_on_tcgplayer = EXCLUDED.unread_on_tcgplayer, last_synced_at = now()""",
                        (r["thread_id"], r["order_number"], r["seller"], r["subject"], r["message_count"], r["last_date"], r["unread"]))
    return stats


def ingest_thread(path: str, dry_run: bool = False) -> dict:
    with open(path, encoding="utf-8", errors="replace") as fh:
        t = parse_msg_thread(fh.read())
    if t is None:
        return {"error": "not a thread page"}
    stats = {"thread_id": t["thread_id"], "order": t["order_number"], "messages": len(t["messages"]), "new": 0, "tracked": True}
    with db_cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '120s'")
        tracked = _tracked_orders(cur)
        if t["order_number"] not in tracked:
            stats["tracked"] = False
            return stats
        if dry_run:
            return stats
        cur.execute("""SELECT t.seller, t.unread_on_tcgplayer FROM tcg_msg_threads t WHERE t.thread_id = %s""", (t["thread_id"],))
        row = cur.fetchone()
        seller = (row["seller"] if row else None) or tracked[t["order_number"]]
        was_unread = bool(row and row["unread_on_tcgplayer"])
        recent_cutoff = dt.date.today() - dt.timedelta(days=NEW_WITHIN_DAYS)
        last_date = max([m["sent_on"] for m in t["messages"] if m["sent_on"]], default=None)
        cur.execute("""
            INSERT INTO tcg_msg_threads (thread_id, order_number, seller, last_date, message_count)
            VALUES (%s, (SELECT order_number FROM tcg_orders WHERE upper(order_number) = %s), %s, %s, %s)
            ON CONFLICT (thread_id) DO UPDATE SET last_synced_at = now(),
                last_date = GREATEST(tcg_msg_threads.last_date, EXCLUDED.last_date)""",
                    (t["thread_id"], t["order_number"], seller, last_date, len(t["messages"])))
        seen_count = {}
        for pos, m in enumerate(t["messages"]):
            key = (m["sender"], m["sent_on"], m["body"])
            seen_count[key] = seen_count.get(key, 0) + 1
            h = hashlib.sha256(f"{t['thread_id']}|{m['sender']}|{m['sent_on']}|{m['body']}|{seen_count[key]}".encode()).hexdigest()
            direction = "out" if (m["sender"] or "").casefold() in BUYER_NAMES else "in"
            # not new when it is ours, or old history of a thread we had already read
            is_old = direction == "in" and not was_unread and m["sent_on"] is not None and m["sent_on"] < recent_cutoff
            cur.execute("""
                INSERT INTO tcg_msg_messages (thread_id, order_number, position, sender, sent_on, body, direction, msg_hash, seen_at)
                VALUES (%s, (SELECT order_number FROM tcg_orders WHERE upper(order_number) = %s), %s, %s, %s, %s, %s, %s,
                        CASE WHEN %s THEN now() END)
                ON CONFLICT (msg_hash) DO UPDATE SET position = EXCLUDED.position
                RETURNING (xmax = 0) AS inserted""",
                        (t["thread_id"], t["order_number"], pos, m["sender"], m["sent_on"], m["body"], direction, h,
                         direction == "out" or is_old))
            stats["new"] += 1 if cur.fetchone()["inserted"] else 0
    return stats


def export_follow_up(outbox: str = DEFAULT_OUTBOX) -> int:
    """Rewrite outbox/follow_up.txt: order number <TAB> seller <TAB> reasons, one line per order."""
    with db_cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '60s'")
        cur.execute("""SELECT f.order_number, o.seller, f.reasons FROM v_tcg_followups f
                       JOIN tcg_orders o ON o.order_number = f.order_number ORDER BY o.ordered_on DESC, f.order_number""")
        rows = cur.fetchall()
    os.makedirs(outbox, exist_ok=True)
    lines = [f"{r['order_number']}\t{r['seller'] or ''}\t{'; '.join(REASON_TEXT.get(x, x) for x in r['reasons'])}" for r in rows]
    tmp = os.path.join(outbox, "follow_up.txt.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + ("\n" if lines else ""))
    os.replace(tmp, os.path.join(outbox, "follow_up.txt"))
    return len(rows)
