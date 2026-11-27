"""
importer/tcg_outbox.py
Outbound side of TCGplayer seller messages (plan: docs/plans/tcg-seller-messages.md, Phase 3).

  export_approved()    approved messages -> outbox/msg_<id>.json for Grok to send
  mark_unknown()       exported more than 24 h ago and no result from Grok -> status 'unknown'
  ingest_msg_result()  inbox/msg_result_<id>.json from Grok -> status sent / failed / unknown

Run by `main.py --tcg-messages-export` (the Approve button, via the desktop API) and by every Sync
(`--tcg-orders-process`). Nothing here ever sends a message to a seller: Grok does that, and only
the exact text in the file. A message is written to the outbox once; a message that has no result
is flagged, never re-sent automatically.

File Grok reads  (outbox/msg_<id>.json):
  {"id", "order_number", "seller", "mode": "new"|"reply", "thread_id", "subject", "response_needed", "body"}
File Grok writes (inbox/msg_result_<id>.json):
  {"id", "status": "sent"|"failed"|"unknown", "sent_at", "error"}
"""

import datetime as dt
import json
import os
import re

from db.connection import db_cursor
from importer.tcg_messages import DEFAULT_OUTBOX

UNKNOWN_AFTER_HOURS = 24


def _log(msg: str):
    print(f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def export_approved(dry_run: bool = False, outbox: str = DEFAULT_OUTBOX) -> dict:
    """Write every approved message as one file in the outbox, then mark it exported."""
    with db_cursor() as cur:
        cur.execute("SET LOCAL statement_timeout = '60s'")
        cur.execute("""SELECT id::text AS id, order_number, seller, mode, thread_id, subject, response_needed, body
                       FROM tcg_msg_outbox WHERE status = 'approved' ORDER BY approved_at, created_at""")
        rows = [dict(r) for r in cur.fetchall()]
    exported = []
    if rows and not dry_run:
        os.makedirs(outbox, exist_ok=True)
    for r in rows:
        if dry_run:
            exported.append(r["id"])
            continue
        payload = {k: r[k] for k in ("id", "order_number", "seller", "mode", "thread_id", "subject", "response_needed", "body")}
        tmp = os.path.join(outbox, f"msg_{r['id']}.json.tmp")
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, os.path.join(outbox, f"msg_{r['id']}.json"))
        with db_cursor() as cur:
            cur.execute("UPDATE tcg_msg_outbox SET status = 'exported', exported_at = now() WHERE id = %s AND status = 'approved'", (r["id"],))
        exported.append(r["id"])
    unknown = mark_unknown(dry_run)
    if exported or unknown:
        _log(f"outbox: {len(exported)} message(s) written for Grok, {unknown} marked unknown")
    return {"exported": len(exported), "unknown": unknown, "note": None if exported else "nothing to send"}


def mark_unknown(dry_run: bool = False) -> int:
    """No result within 24 h of being handed to Grok: flag it (do not re-send)."""
    if dry_run:
        return 0
    with db_cursor() as cur:
        cur.execute("""UPDATE tcg_msg_outbox SET status = 'unknown', error = 'No result from Grok within 24 hours'
                       WHERE status = 'exported' AND exported_at < now() - make_interval(hours => %s)""", (UNKNOWN_AFTER_HOURS,))
        return cur.rowcount


def _parse_time(v) -> dt.datetime:
    s = str(v or "").strip()
    if s:
        try:
            d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
            return d if d.tzinfo else d.astimezone()
        except ValueError:
            pass
        for fmt in ("%Y-%m-%d %H:%M", "%m/%d/%Y %H:%M", "%m/%d/%Y %I:%M %p", "%B %d, %Y %I:%M %p"):
            try:
                return dt.datetime.strptime(s, fmt).astimezone()
            except ValueError:
                continue
    return dt.datetime.now().astimezone()


def ingest_msg_result(path: str, dry_run: bool = False, outbox: str = DEFAULT_OUTBOX) -> dict:
    """One Grok result file. Idempotent: a message already 'sent' is left alone."""
    try:
        with open(path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as e:
        return {"error": f"unreadable result file: {e}"}
    mid = str(data.get("id") or "").strip()
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", mid):
        m = re.search(r"msg_result_([0-9a-fA-F-]{36})", os.path.basename(path))
        mid = m.group(1) if m else ""
    if not mid:
        return {"error": "result file has no message id"}
    status = str(data.get("status") or "").strip().lower()
    if status not in ("sent", "failed", "unknown"):
        status = "unknown"
    error = (str(data.get("error") or "").strip() or None)
    if status == "failed" and not error:
        error = "Grok reported the send failed without a reason"
    stats = {"id": mid, "status": status, "found": False}
    if dry_run:
        return stats
    with db_cursor() as cur:
        cur.execute("""UPDATE tcg_msg_outbox SET status = %s, sent_at = CASE WHEN %s = 'sent' THEN %s END, error = %s
                       WHERE id = %s AND status IN ('approved', 'exported', 'unknown', 'failed')""",
                    (status, status, _parse_time(data.get("sent_at")), (error or "")[:500] or None, mid))
        stats["found"] = cur.rowcount > 0
    if stats["found"]:
        # the outbox file has done its job; remove it so Grok can never pick it up twice
        f = os.path.join(outbox, f"msg_{mid}.json")
        try:
            os.remove(f)
        except OSError:
            pass
    return stats
