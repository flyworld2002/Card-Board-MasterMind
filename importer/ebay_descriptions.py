"""
importer/ebay_descriptions.py — buyer-facing navigation rendered into eBay
listing descriptions (family strip, era hub-and-spoke, era index), built
from listing_templates relationships (migration 020) and card_sets.

render_description() is a pure function over DB state — no eBay calls, unit-
testable against seeded rows (see test_render_description.py at repo root).
Pushing the rendered HTML live is sync_description() (added alongside
--ebay-sync-descriptions), which reuses ebay_create_listing.py's revise
XML-build core rather than forking a copy.

See docs/plans/listing-pricing-system.md, "Plan: eBay description
navigation system", and migration 020.
"""

import hashlib
import re

from db.connection import db_cursor
from importer.ebay import _post
from importer.ebay_auth import get_user_token
from importer.ebay_create_listing import (
    _load_template, build_revise_item_xml, fetch_listing_metadata, ALL_METADATA_FIELDS,
)

TOKEN_PATTERN = re.compile(r"\{\{(\w+)\}\}")

FINISH_LABELS = {
    "non_holo": "Non-Holo",
    "reverse_holo": "Reverse Holo",
    "poke_ball": "Poké Ball",
    "master_ball": "Master Ball",
    "ultra_rare": "Ultra Rare",
}

# Card tile width (family strip / era grid) — nav_image_url photos come
# straight from eBay's own gallery (or a custom upload), full-size, so
# without a cap a 2-3 tile row stretches each image to roughly half the
# description's width. Fixed px on the <td> (not just the <img>) so the
# table column itself doesn't stretch to fill the row.
NAV_TILE_WIDTH = 140


def _item_url(listing_id: str) -> str:
    return f"https://www.ebay.com/itm/{listing_id}"


def _resolve_finish_match(cur, set_id: str, finish_kind: str) -> dict | None:
    """The template to link to for a given (set, finish): the ONE template
    matching both set_id and finish_kind, falling back to that set's
    is_set_primary template.

    Falls back on 0 OR 2+ finish matches, not a bare LIMIT 1 — reviewed
    with Fei: two templates sharing a set+finish is an accepted real
    scenario (e.g. a set's reverse holos split across two listings), not
    an error state, and should route to the set's declared front door
    rather than picking one arbitrarily (which could also flap between
    renders and defeat sync_description()'s hash-based skip)."""
    if not set_id:
        return None

    rows = []
    if finish_kind:
        cur.execute(
            """
            SELECT * FROM listing_templates
            WHERE set_id = %s AND finish_kind = %s AND listing_id IS NOT NULL
            """,
            (set_id, finish_kind),
        )
        rows = cur.fetchall()

    if len(rows) == 1:
        return rows[0]

    cur.execute(
        """
        SELECT * FROM listing_templates
        WHERE set_id = %s AND is_set_primary AND listing_id IS NOT NULL
        LIMIT 1
        """,
        (set_id,),
    )
    return cur.fetchone()


def _era_base_set(cur, series: str) -> dict | None:
    """Era's hub set is DERIVED, not configured: the set whose name equals
    its own series (confirmed to hold for SwSh / SV / Mega Evolution).
    Eras with no such set simply don't appear anywhere nav renders."""
    if not series:
        return None
    cur.execute(
        "SELECT * FROM card_sets WHERE series = %s AND name = series LIMIT 1",
        (series,),
    )
    return cur.fetchone()


# ══════════════════════════════════════════════════════════════════════════════
# Block markup — inline styles only, table-friendly, no JS, no external CSS,
# https images only. NOT finalized — see milestone-4 sanitizer test in the
# plan doc; kept isolated in these three helpers so a markup fix is a
# one-place change.
# ══════════════════════════════════════════════════════════════════════════════

def _nav_cell_html(label: str, url: str | None, image_url: str | None, highlighted: bool = False) -> str:
    """Card-style tile. Fei's images carry their own label/branding, so
    when nav_image_url is set the image IS the tile — no text row under
    it. Falls back to a plain text tile when there's no image yet (e.g.
    before backfill/upload runs), so every listing stays usable in the
    meantime. "You're here" can't be baked into the (shared, reused-
    across-listings) image itself, so it's a small overlay badge instead
    of text, added dynamically at render time regardless of image state."""
    if image_url:
        inner = f'<img src="{image_url}" alt="{label}" style="width:100%;display:block;">'
    else:
        inner = (f'<div style="padding:24px 8px;text-align:center;font-family:sans-serif;'
                  f'font-size:13px;color:#333;">{label}</div>')

    badge = ('<div style="position:absolute;top:6px;right:6px;background:rgba(0,0,0,0.75);'
             'color:#fff;font-size:10px;font-family:sans-serif;padding:2px 7px;'
             'border-radius:10px;">You&rsquo;re here</div>') if highlighted else ""

    tile = f'<div style="position:relative;">{inner}{badge}</div>'
    if url:
        tile = f'<a href="{url}" style="display:block;text-decoration:none;">{tile}</a>'

    ring = "2px solid #333" if highlighted else "1px solid #e0e0e0"
    # max-width, not a fixed width, on both the td and the inner card so
    # the tile caps at NAV_TILE_WIDTH on desktop but can still shrink
    # smaller on narrow/mobile screens instead of forcing overflow.
    return (f'<td style="padding:6px;max-width:{NAV_TILE_WIDTH}px;">'
            f'<div style="max-width:{NAV_TILE_WIDTH}px;border:{ring};border-radius:10px;'
            f'overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,0.12);">{tile}</div></td>')


def _nav_block_html(title: str, cells: list[str]) -> str:
    # No width:100% on the table — tiles stay fixed-width (NAV_TILE_WIDTH)
    # and the table is centered via margin:0 auto rather than stretched
    # across the whole description column.
    rows = "".join(cells)
    return (f'<div style="margin:14px 0;text-align:center;"><div style="font-family:sans-serif;'
            f'font-weight:bold;margin-bottom:8px;">{title}</div>'
            f'<table style="border-collapse:separate;border-spacing:0;margin:0 auto;">'
            f'<tr>{rows}</tr></table></div>')


def _banner_html(text: str, url: str) -> str:
    return (f'<div style="margin:12px 0;"><a href="{url}" style="display:block;'
            f'padding:10px;text-align:center;background:#f5f5f5;border:1px solid #ccc;'
            f'font-family:sans-serif;font-weight:bold;text-decoration:none;color:#333;">'
            f'{text} &rarr;</a></div>')


def _chip_row_html(chips: list[str]) -> str:
    return f'<div style="margin:12px 0;">{"".join(chips)}</div>'


def _chip_html(label: str, url: str | None, highlighted: bool) -> str:
    text = f"{label} (you're here)" if highlighted else label
    style = ("display:inline-block;margin:2px;padding:4px 10px;border-radius:12px;"
              "font-family:sans-serif;font-size:12px;text-decoration:none;")
    if highlighted:
        style += "background:#333;color:#fff;"
    else:
        style += "background:#eee;color:#333;"
    if url:
        return f'<a href="{url}" style="{style}">{text}</a>'
    return f'<span style="{style}">{text}</span>'


# ══════════════════════════════════════════════════════════════════════════════
# Token renderers
# ══════════════════════════════════════════════════════════════════════════════

def _render_family_nav(cur, template: dict) -> str:
    if not template.get("set_id"):
        return ""
    cur.execute(
        """
        SELECT * FROM listing_templates
        WHERE set_id = %s AND show_in_nav AND listing_id IS NOT NULL
        ORDER BY nav_rank NULLS LAST, family_label
        """,
        (template["set_id"],),
    )
    siblings = cur.fetchall()
    if len(siblings) < 2:
        return ""

    cells = []
    for s in siblings:
        is_self = s["id"] == template["id"]
        cells.append(_nav_cell_html(
            label=s["family_label"] or FINISH_LABELS.get(s["finish_kind"], s["finish_kind"] or "Listing"),
            url=None if is_self else _item_url(s["listing_id"]),
            image_url=s["nav_image_url"],
            highlighted=is_self,
        ))
    return _nav_block_html("Shop this set", cells)


def _render_era_hub_link(cur, template: dict) -> str:
    if not template.get("set_id"):
        return ""
    cur.execute("SELECT series FROM card_sets WHERE id = %s", (template["set_id"],))
    row = cur.fetchone()
    series = row["series"] if row else None
    base_set = _era_base_set(cur, series)
    if not base_set or base_set["id"] == template["set_id"]:
        return ""  # no era, or I AM the hub — no self-link

    hub_template = _resolve_finish_match(cur, base_set["id"], template.get("finish_kind"))
    if not hub_template or not hub_template.get("listing_id"):
        return ""
    return _banner_html(f"Shop all {series} era sets", _item_url(hub_template["listing_id"]))


def _render_era_nav(cur, template: dict) -> str:
    if not template.get("set_id"):
        return ""
    cur.execute("SELECT * FROM card_sets WHERE id = %s", (template["set_id"],))
    my_set = cur.fetchone()
    if not my_set or not my_set["series"]:
        return ""
    base_set = _era_base_set(cur, my_set["series"])
    if not base_set or base_set["id"] != my_set["id"]:
        return ""  # era_nav only renders on the hub itself

    cur.execute(
        "SELECT * FROM card_sets WHERE series = %s AND id != %s ORDER BY release_year, name",
        (my_set["series"], my_set["id"]),
    )
    other_sets = cur.fetchall()

    cells = []
    for s in other_sets:
        match = _resolve_finish_match(cur, s["id"], template.get("finish_kind"))
        if not match or not match.get("listing_id"):
            continue
        cells.append(_nav_cell_html(label=s["name"], url=_item_url(match["listing_id"]),
                                     image_url=match["nav_image_url"]))
    if not cells:
        return ""
    return _nav_block_html(f"Shop the {my_set['series']} era", cells)


def _render_era_index(cur, template: dict) -> str:
    cur.execute("SELECT DISTINCT series FROM card_sets WHERE series IS NOT NULL ORDER BY series")
    all_series = [r["series"] for r in cur.fetchall()]

    my_series = None
    if template.get("set_id"):
        cur.execute("SELECT series FROM card_sets WHERE id = %s", (template["set_id"],))
        row = cur.fetchone()
        my_series = row["series"] if row else None

    chips = []
    for series in all_series:
        base_set = _era_base_set(cur, series)
        if not base_set:
            continue
        match = _resolve_finish_match(cur, base_set["id"], template.get("finish_kind"))
        if not match or not match.get("listing_id"):
            continue
        is_self = series == my_series
        chips.append(_chip_html(series, None if is_self else _item_url(match["listing_id"]), is_self))
    if not chips:
        return ""
    return _chip_row_html(chips)


def _render_simple_tokens(cur, template: dict) -> dict:
    """set_name / series_name — plain-text tokens the Spoke/Hub starter
    skeletons rely on, resolved the same substitution pass as the nav
    blocks."""
    if not template.get("set_id"):
        return {"set_name": "", "series_name": ""}
    cur.execute("SELECT name, series FROM card_sets WHERE id = %s", (template["set_id"],))
    row = cur.fetchone()
    if not row:
        return {"set_name": "", "series_name": ""}
    return {"set_name": row["name"] or "", "series_name": row["series"] or ""}


TOKEN_RENDERERS = {
    "family_nav": _render_family_nav,
    "era_hub_link": _render_era_hub_link,
    "era_nav": _render_era_nav,
    "era_index": _render_era_index,
}


def preview_description(template_id: str, source_html: str | None = None) -> dict:
    """Read-only render — no DB writes, no eBay call. Accepts an optional
    draft source_html so the UI can preview UNSAVED textarea edits before
    saving; falls back to the template's stored description_html when
    omitted."""
    with db_cursor() as cur:
        template = _load_template(cur, template_id)
        if template is None:
            return {"error": "no such template"}
        html = render_description(template, cur, source_html=source_html)
    return {"html": html}


def render_description(template: dict, cur, source_html: str | None = None) -> str:
    """Substitutes every {{token}} in source_html (defaults to
    template['description_html']) against current DB state. Unknown or
    absent tokens render as empty string; a template with no tokens
    renders unchanged — the safe default for every template Fei hasn't
    opted into nav for."""
    source = source_html if source_html is not None else (template.get("description_html") or "")
    if not source:
        return source

    simple = _render_simple_tokens(cur, template)
    cache = {}

    def substitute(match):
        token = match.group(1)
        if token in simple:
            return simple[token]
        if token not in TOKEN_RENDERERS:
            return ""
        if token not in cache:
            cache[token] = TOKEN_RENDERERS[token](cur, template)
        return cache[token]

    return TOKEN_PATTERN.sub(substitute, source)


# ══════════════════════════════════════════════════════════════════════════════
# Push — hash-gated ReviseFixedPriceItem, shares its XML-build core with
# ebay_create_listing.revise_listing_metadata() rather than forking a copy.
# ══════════════════════════════════════════════════════════════════════════════

def resolve_sync_scope(cur, template_id: str = None, set_name: str = None,
                        era: str = None, all_scope: bool = False) -> list[str]:
    """listing_templates.id values for one --ebay-sync-descriptions scope
    flag. Always excludes templates with no live listing_id — nothing to
    push to yet."""
    if template_id:
        cur.execute("SELECT id FROM listing_templates WHERE id = %s AND listing_id IS NOT NULL",
                    (template_id,))
    elif set_name:
        cur.execute(
            """
            SELECT lt.id FROM listing_templates lt
            JOIN card_sets cs ON cs.id = lt.set_id
            WHERE cs.name = %s AND lt.listing_id IS NOT NULL
            """,
            (set_name,),
        )
    elif era:
        cur.execute(
            """
            SELECT lt.id FROM listing_templates lt
            JOIN card_sets cs ON cs.id = lt.set_id
            WHERE cs.series = %s AND lt.listing_id IS NOT NULL
            """,
            (era,),
        )
    elif all_scope:
        cur.execute("SELECT id FROM listing_templates WHERE listing_id IS NOT NULL")
    else:
        return []
    return [r["id"] for r in cur.fetchall()]


def sync_description(template_id: str, account_num: int = 1, dry_run: bool = False) -> dict:
    """Renders template_id's description_html against current DB state and
    pushes it live via ReviseFixedPriceItem if it differs from what was
    last pushed. sha256(rendered) vs pushed_description_hash makes
    --all --dry-run cheap and shows exactly which listings would actually
    change before anything is sent — re-renders fresh every call, so it's
    correct even when what changed was a SIBLING template (family/era
    membership), not this template's own row.

    Reuses build_revise_item_xml() from ebay_create_listing.py — the
    RENDERED html is sent as the description, but unlike
    revise_listing_metadata(), description_html itself is never written
    back: that column is Fei's authored SOURCE, rendering is a derived
    view. Only description_live_html / pushed_description_hash /
    description_pushed_at update, and only after a confirmed eBay
    success — same "never write DB before a confirmed eBay success" rule
    every other push in this feature follows.
    """
    with db_cursor() as cur:
        template = _load_template(cur, template_id)
        if template is None:
            return {"template_id": template_id, "pushed": False, "dry_run": dry_run,
                     "error": "no such template"}
        if not template["listing_id"]:
            return {"template_id": template_id, "pushed": False, "dry_run": dry_run,
                     "error": "template has no live listing_id yet"}

        rendered = render_description(template, cur)
        rendered_hash = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
        changed = rendered_hash != template.get("pushed_description_hash")

        if not changed:
            return {"template_id": template_id, "pushed": False, "dry_run": dry_run,
                     "changed": False, "rendered_html": rendered}

        if dry_run:
            return {"template_id": template_id, "pushed": False, "dry_run": True,
                     "changed": True, "rendered_html": rendered}

        merged = {k: template.get(k) for k in ALL_METADATA_FIELDS}
        merged["description_html"] = rendered

        token = get_user_token(account_num)
        xml = build_revise_item_xml(template["listing_id"], merged, token)
        _post("ReviseFixedPriceItem", xml, account_num=account_num)

        cur.execute(
            """
            UPDATE listing_templates
            SET description_live_html = %s, pushed_description_hash = %s,
                description_pushed_at = now()
            WHERE id = %s
            """,
            (rendered, rendered_hash, template_id),
        )

    return {"template_id": template_id, "pushed": True, "dry_run": False,
            "changed": True, "rendered_html": rendered}


# ══════════════════════════════════════════════════════════════════════════════
# One-time (re-runnable) nav image backfill
# ══════════════════════════════════════════════════════════════════════════════

def backfill_nav_images(account_num: int = 1, force: bool = False) -> dict:
    """For every template with a live listing_id, fills nav_image_url from
    that listing's first eBay-hosted gallery photo via GetItem
    (fetch_listing_metadata()'s existing PictureDetails parse — no second
    GetItem call path added). Only fills NULLs by default, so it's safe to
    re-run as new listings go live; force=True re-fetches and overwrites
    every in-scope template's nav_image_url regardless."""
    with db_cursor() as cur:
        if force:
            cur.execute("SELECT id, listing_id FROM listing_templates WHERE listing_id IS NOT NULL")
        else:
            cur.execute(
                "SELECT id, listing_id FROM listing_templates "
                "WHERE listing_id IS NOT NULL AND nav_image_url IS NULL"
            )
        targets = cur.fetchall()

    filled, skipped, errors = [], [], []
    for row in targets:
        try:
            fields = fetch_listing_metadata(row["listing_id"], account_num=account_num)
        except Exception as e:
            errors.append({"template_id": row["id"], "error": str(e)})
            continue

        url = fields.get("first_picture_url")
        if not url:
            skipped.append(row["id"])
            continue

        with db_cursor() as cur:
            cur.execute(
                "UPDATE listing_templates SET nav_image_url = %s, updated_at = now() WHERE id = %s",
                (url, row["id"]),
            )
        filled.append(row["id"])

    return {"filled": filled, "skipped_no_picture": skipped, "errors": errors}


# ══════════════════════════════════════════════════════════════════════════════
# Reusable section/layout library (migration 021) — supersedes the old
# hardcoded DESCRIPTION_PRESETS constant. 'layout' rows are whole-
# description starters (Insert-layout dropdown, REPLACES the textarea);
# 'section' rows are small reusable blocks (Insert-section dropdown,
# inserts AT CURSOR). Both may contain {{tokens}}, substituted by the same
# render_description() pass as everything else — no separate rendering
# path for library content.
# ══════════════════════════════════════════════════════════════════════════════

def list_description_sections() -> dict:
    """{key: {label, html, kind}} — the shape /api/description-presets has
    always returned (now DB-backed instead of a code constant), ordered by
    sort_order so the dropdown's item order is editable without a
    deploy."""
    with db_cursor() as cur:
        cur.execute("SELECT key, label, html, kind FROM description_sections ORDER BY sort_order, label")
        rows = cur.fetchall()
    return {r["key"]: {"label": r["label"], "html": r["html"], "kind": r["kind"]} for r in rows}


def list_description_sections_full() -> list[dict]:
    """Full rows (id, sort_order, updated_at included) for the section
    manager UI — list_description_sections()'s trimmed shape is for the
    dropdown, this is for CRUD."""
    with db_cursor() as cur:
        cur.execute(
            "SELECT id, key, label, html, kind, sort_order, updated_at "
            "FROM description_sections ORDER BY sort_order, label"
        )
        return cur.fetchall()


def create_description_section(key: str, label: str, html: str, kind: str = "section",
                                sort_order: int = 0) -> dict:
    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO description_sections (key, label, html, kind, sort_order)
            VALUES (%s, %s, %s, %s, %s) RETURNING id, key, label, html, kind, sort_order, updated_at
            """,
            (key, label, html, kind, sort_order),
        )
        return cur.fetchone()


_SECTION_FIELDS = ("key", "label", "html", "kind", "sort_order")


def update_description_section(section_id: str, **fields) -> dict:
    """Partial update — same pattern as set_manual_listing_metadata():
    only columns present in fields are touched."""
    cols = [f for f in _SECTION_FIELDS if f in fields]
    if not cols:
        return {"id": section_id, "updated": []}
    set_clause = ", ".join(f"{c} = %s" for c in cols)
    values = [fields[c] for c in cols]
    with db_cursor() as cur:
        cur.execute(
            f"UPDATE description_sections SET {set_clause}, updated_at = now() "
            f"WHERE id = %s RETURNING id, key, label, html, kind, sort_order, updated_at",
            values + [section_id],
        )
        row = cur.fetchone()
    if row is None:
        return {"id": section_id, "error": "no such section"}
    return row


def delete_description_section(section_id: str) -> dict:
    with db_cursor() as cur:
        cur.execute("DELETE FROM description_sections WHERE id = %s RETURNING id", (section_id,))
        row = cur.fetchone()
    return {"id": section_id, "deleted": row is not None}
