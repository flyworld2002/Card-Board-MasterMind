"""
importer/card_photos.py — per-copy photo library (card_photos /
card_photo_details, migration 041). See docs/plans/listing-pricing-system.md.

Replaces the old assumption that one queued roster row needs exactly one
freshly-uploaded picture (listing_card_assignments.eps_picture_url,
still kept as a legacy fallback — see resolve_photo_urls()). A photo
group is scoped to one card_variants row (not card_master — a holo and
reverse-holo print of the same card look physically different) and can
carry a front photo plus any number of additional photos (back,
close-up, etc.). Multiple groups per variant represent multiple
physical copies of that same print, each needing its own distinct set
(condition/centering differs per copy).

Which group auto-applies when a card is added to a listing (rather than
requiring a manual pick every time) is resolved by the
default_card_photo_id() Postgres RPC (migration 041), called directly
from the browser at insert time — not duplicated here in Python.
"""

from db.connection import db_cursor
from importer.ebay_pictures import upload_picture_bytes, upload_picture_from_url


def _upload(source_url: str = None, image_bytes: bytes = None, filename: str = None,
            account_num: int = 1) -> str:
    """Shared upload-one-photo-to-EPS helper — same source_url XOR
    image_bytes+filename contract stage_card_picture() already uses."""
    if source_url:
        return upload_picture_from_url(source_url, account_num=account_num)
    return upload_picture_bytes(image_bytes, filename or "card.jpg", account_num=account_num)


def list_card_photos(variant_id: str) -> list[dict]:
    """Every existing photo group for one variant, front photo + label +
    ordered additional photos — what the "Manage photos" modal offers to
    reuse instead of uploading again."""
    with db_cursor() as cur:
        cur.execute(
            "SELECT id, front_eps_url, label, has_additional, source_finish_kind, created_at "
            "FROM card_photos WHERE variant_id = %s ORDER BY created_at DESC",
            (variant_id,),
        )
        groups = cur.fetchall()
        if not groups:
            return []

        group_ids = [g["id"] for g in groups]
        cur.execute(
            "SELECT card_photo_id, eps_url, label, sort_order FROM card_photo_details "
            "WHERE card_photo_id = ANY(%s::uuid[]) ORDER BY card_photo_id, sort_order",
            (group_ids,),
        )
        details_by_group: dict[str, list[dict]] = {}
        for d in cur.fetchall():
            details_by_group.setdefault(str(d["card_photo_id"]), []).append(
                {"eps_url": d["eps_url"], "label": d["label"], "sort_order": d["sort_order"]}
            )

    for g in groups:
        g["additional"] = details_by_group.get(str(g["id"]), [])
    return groups


def create_card_photo(variant_id: str, front_source_url: str = None, front_bytes: bytes = None,
                       front_filename: str = None, label: str = None,
                       additional: list[dict] = None, source_finish_kind: str = None,
                       account_num: int = 1) -> dict:
    """Uploads the front photo (+ every photo in `additional`, each
    {"source_url"|"image_bytes"+"filename", "label"}) to EPS, then
    inserts the card_photos header + card_photo_details rows. Every URL
    stored is already EPS-hosted — eBay's Trading API doesn't accept
    arbitrary external URLs for variation pictures, same rule
    stage_card_picture() already follows.

    source_finish_kind is the CURRENT template's finish_kind at the time
    this group is created — feeds default_card_photo_id()'s "prefer a
    same-finish-kind source" priority when this group is later offered
    as a default on some other listing.
    """
    if not front_source_url and not front_bytes:
        return {"created": False, "error": "must provide front_source_url or front_bytes"}

    try:
        front_url = _upload(front_source_url, front_bytes, front_filename, account_num)
    except Exception as e:
        return {"created": False, "error": f"EPS upload failed (front): {e}"}

    additional = additional or []
    detail_urls = []
    for i, extra in enumerate(additional):
        try:
            url = _upload(extra.get("source_url"), extra.get("image_bytes"),
                           extra.get("filename"), account_num)
        except Exception as e:
            return {"created": False, "error": f"EPS upload failed (additional photo {i + 1}): {e}"}
        detail_urls.append((url, extra.get("label")))

    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO card_photos (variant_id, front_eps_url, label, has_additional, source_finish_kind)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id, front_eps_url, label, has_additional, source_finish_kind, created_at
            """,
            (variant_id, front_url, label, bool(detail_urls), source_finish_kind),
        )
        group = cur.fetchone()

        for sort_order, (url, detail_label) in enumerate(detail_urls):
            cur.execute(
                "INSERT INTO card_photo_details (card_photo_id, eps_url, label, sort_order) "
                "VALUES (%s, %s, %s, %s)",
                (group["id"], url, detail_label, sort_order),
            )

    group["additional"] = [{"eps_url": u, "label": l, "sort_order": i} for i, (u, l) in enumerate(detail_urls)]
    return {"created": True, "card_photo": group}


def assign_card_photo(row_id: str, card_photo_id: str) -> dict:
    """Points a roster row's card_photo_id at an existing group — zero EPS
    calls, the whole point of this feature. Allowed for 'queued' rows
    (rides along automatically on the next promotion push) and 'active'
    rows (takes effect only once push_card_photo_live() is called for
    this row — reassigning here alone does not touch the live eBay
    listing). Validates the group actually belongs to this row's
    variant — picking a photo group from a different variant would
    silently show the wrong card's picture."""
    with db_cursor() as cur:
        cur.execute(
            "SELECT id, status, variant_id FROM listing_card_assignments WHERE id = %s", (row_id,)
        )
        row = cur.fetchone()
        if row is None:
            return {"row_id": row_id, "assigned": False, "error": "no such roster row"}
        if row["status"] not in ("queued", "active"):
            return {"row_id": row_id, "assigned": False,
                     "error": f"row is {row['status']!r} — pictures can only be assigned for "
                              f"'queued' or 'active' cards"}

        cur.execute("SELECT variant_id FROM card_photos WHERE id = %s", (card_photo_id,))
        photo = cur.fetchone()
        if photo is None:
            return {"row_id": row_id, "assigned": False, "error": "no such photo group"}
        if str(photo["variant_id"]) != str(row["variant_id"]):
            return {"row_id": row_id, "assigned": False,
                     "error": "that photo group belongs to a different card variant"}

        cur.execute(
            "UPDATE listing_card_assignments SET card_photo_id = %s, updated_at = now() WHERE id = %s",
            (card_photo_id, row_id),
        )

    return {"row_id": row_id, "assigned": True, "card_photo_id": card_photo_id}


def resolve_photo_urls(cur, row: dict) -> list[str]:
    """Ordered URL list to push for one roster row (dict with
    card_photo_id/eps_picture_url keys, e.g. straight from
    listing_card_assignments or resolve_listing_prices()) — front photo
    first, then every card_photo_details row in sort_order. card_photo_id
    wins when set; falls back to the legacy single eps_picture_url column
    for rows staged before this feature existed; empty list if neither
    is set (no picture at all, same as today)."""
    if row.get("card_photo_id"):
        cur.execute(
            "SELECT front_eps_url FROM card_photos WHERE id = %s", (row["card_photo_id"],)
        )
        photo = cur.fetchone()
        if photo is None:
            return []
        urls = [photo["front_eps_url"]]
        cur.execute(
            "SELECT eps_url FROM card_photo_details WHERE card_photo_id = %s ORDER BY sort_order",
            (row["card_photo_id"],),
        )
        urls.extend(d["eps_url"] for d in cur.fetchall())
        return urls
    if row.get("eps_picture_url"):
        return [row["eps_picture_url"]]
    return []


# ══════════════════════════════════════════════════════════════════════════════
# One-time (re-runnable) backfill: pull already-live pictures back from eBay
# ══════════════════════════════════════════════════════════════════════════════

def backfill_card_photos_from_ebay(account_num: int = 1, listing_id: str = None,
                                    force: bool = False, dry_run: bool = False) -> dict:
    """One-time (re-runnable) backfill for roster rows that went live
    before card_photos existed — their real eBay picture was attached
    directly through eBay's own tools, so nothing in this codebase ever
    recorded it. Confirmed 2026-08-17: card_photos had 0 rows despite
    9,470 listing_card_assignments rows already status='active'.

    Rows are grouped by listing_id so ONE GetItem call covers every
    active roster row on that listing (65 unique listings backed all
    9,245 active rows when this was written, not 9,245 separate calls).
    For each listing, matches Variations/Pictures/VariationSpecific-
    PictureSet entries by VariationSpecificValue against
    platform_listings.external_id (the exact string every promotion
    already writes there) to find that row's real, currently-live
    picture URL(s) — front first, any extra photos after, same order
    eBay itself displays them in.

    No EPS upload happens — every URL pulled is already eBay-hosted
    (i.ebayimg.com), so this only reads via GetItem and writes to
    card_photos/card_photo_details/listing_card_assignments. Reuses an
    existing card_photos row for a variant_id when one already has the
    same front_eps_url, so re-running this (or a variant being active on
    more than one listing) doesn't create duplicate groups.

    Writes directly to listing_card_assignments.card_photo_id for
    already-'active' rows — deliberately bypasses assign_card_photo()'s
    status='queued' restriction, since that guard exists for the
    user-facing manual-reassign flow, not this backfill.

    Only fills rows with card_photo_id IS NULL by default; force=True
    re-derives and overwrites every in-scope row regardless.
    dry_run=True does the GetItem reads and the matching but no DB
    writes — returns what WOULD be filled.
    """
    from importer.ebay_variations_xml import fetch_item
    from importer.ebay import _find, _findall, _text

    with db_cursor() as cur:
        # pl.template_id is NULL on almost every real row (only ever set
        # for a handful of rows pushed a specific way) — join to
        # listing_templates via listing_id (the eBay item ID), which
        # every active platform_listings row actually has, instead.
        #
        # pl.status IN ('active', 'out_of_stock') deliberately excludes
        # only 'delisted' — an out_of_stock variation is still live on
        # eBay (qty 0, not removed), so GetItem still finds its real
        # picture; a delisted one's listing_id may no longer resolve at
        # all. First cut of this backfill scoped to pl.status='active'
        # only and missed 227 out_of_stock rows as a result — widened
        # after confirming out_of_stock is a "still listed, just sold
        # out" state (see the "self-heal status" commit), not "removed."
        query = """
            SELECT lca.id AS row_id, pl.listing_id, pl.external_id, pl.variant_id, lt.finish_kind
            FROM listing_card_assignments lca
            JOIN platform_listings pl ON pl.id = lca.platform_listing_id
            JOIN listing_templates lt ON lt.listing_id = pl.listing_id
            WHERE lca.status = 'active' AND pl.status IN ('active', 'out_of_stock')
        """
        params = []
        if not force:
            query += " AND lca.card_photo_id IS NULL"
        if listing_id:
            query += " AND pl.listing_id = %s"
            params.append(listing_id)
        query += " ORDER BY pl.listing_id"
        cur.execute(query, params)
        targets = cur.fetchall()

    by_listing: dict[str, list[dict]] = {}
    for row in targets:
        by_listing.setdefault(row["listing_id"], []).append(row)

    filled, skipped_no_match, errors = [], [], []
    listings_fetched = 0

    for lid, rows in by_listing.items():
        try:
            item = fetch_item(lid, account_num=account_num)
        except Exception as e:
            for row in rows:
                errors.append({"row_id": row["row_id"], "listing_id": lid, "error": str(e)})
            continue
        listings_fetched += 1

        pics_by_value: dict[str, list[str]] = {}
        variations_node = _find(item, "Variations")
        pictures_node = _find(variations_node, "Pictures") if variations_node is not None else None
        if pictures_node is not None:
            for vsp in _findall(pictures_node, "VariationSpecificPictureSet"):
                value = _text(vsp, "VariationSpecificValue")
                if not value:
                    continue
                urls = [el.text for el in _findall(vsp, "PictureURL") if el.text]
                if urls:
                    pics_by_value[value] = urls

        for row in rows:
            urls = pics_by_value.get(row["external_id"])
            if not urls:
                skipped_no_match.append({"row_id": row["row_id"], "listing_id": lid,
                                          "external_id": row["external_id"]})
                continue

            if dry_run:
                filled.append({"row_id": row["row_id"], "variant_id": row["variant_id"],
                                "front_eps_url": urls[0], "dry_run": True})
                continue

            with db_cursor() as cur:
                cur.execute(
                    "SELECT id FROM card_photos WHERE variant_id = %s AND front_eps_url = %s",
                    (row["variant_id"], urls[0]),
                )
                existing = cur.fetchone()
                if existing:
                    card_photo_id = existing["id"]
                else:
                    cur.execute(
                        """
                        INSERT INTO card_photos (variant_id, front_eps_url, has_additional, source_finish_kind)
                        VALUES (%s, %s, %s, %s)
                        RETURNING id
                        """,
                        (row["variant_id"], urls[0], len(urls) > 1, row["finish_kind"]),
                    )
                    card_photo_id = cur.fetchone()["id"]
                    for sort_order, url in enumerate(urls[1:]):
                        cur.execute(
                            "INSERT INTO card_photo_details (card_photo_id, eps_url, sort_order) "
                            "VALUES (%s, %s, %s)",
                            (card_photo_id, url, sort_order),
                        )

                cur.execute(
                    "UPDATE listing_card_assignments SET card_photo_id = %s, updated_at = now() WHERE id = %s",
                    (card_photo_id, row["row_id"]),
                )

            filled.append({"row_id": row["row_id"], "variant_id": row["variant_id"],
                            "card_photo_id": card_photo_id, "front_eps_url": urls[0]})

    return {
        "filled": filled,
        "skipped_no_match": skipped_no_match,
        "errors": errors,
        "listings_fetched": listings_fetched,
        "listings_total": len(by_listing),
    }
