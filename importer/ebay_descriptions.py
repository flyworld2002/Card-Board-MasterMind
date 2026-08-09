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

# Item-template placeholder syntax inside a custom {{item_*}} block (a
# description_sections row with kind='item_template') — deliberately a
# separate, smaller substitution pass from TOKEN_PATTERN, run BEFORE the
# item's rendered HTML is embedded into the outer block, never re-entered
# by the outer re.sub() (replacement text is never re-scanned), so there's
# no collision risk between the two token namespaces despite sharing {{ }}
# syntax.
_ITEM_PLACEHOLDER_PATTERN = re.compile(r"\{\{item_(\w+)\}\}")

# Description nav theme (migration 023, description_theme_settings) —
# colors, sizing, and button/label text for the family_nav/era_nav/
# era_index renderer. DEFAULT_THEME is the fallback for any key missing
# from the DB (deleted row, fresh install, etc.) so rendering never
# breaks; _load_theme() merges DB overrides on top of it. Originally
# hardcoded (Deep Sea theme, Fei 8/08) — moved to a DB table so small
# tweaks (especially text) don't require a code change + deploy each
# time.
#
# Migration 028 (8/09) added theme_key scoping — description_theme_settings
# rows are now keyed by (theme_key, key) instead of just key, so different
# shops/listing groups can each run their own theme instead of one theme
# applying everywhere. listing_templates.theme_key selects which one a
# given template renders with; NULL means 'default'.
DEFAULT_THEME = {
    "color_bg": "#0a1f38",
    "color_panel": "#12365c",
    "color_border": "#1d4d7a",
    "color_cyan": "#3fc3e8",
    "color_text_light": "#eaf6fb",
    "color_text_muted": "#7fa8c9",
    "color_text_dim": "#9db2c6",
    "nav_tile_width": "140",
    "text_view_listing": "View listing",
    "text_viewing_this": "Viewing this",
    "text_family_nav_title": "Shop this set",
    "text_era_list_link": "Shop this set",
    "text_era_nav_subtitle": "Same finish, other sets:",
    "text_youre_here_suffix": "(you're here)",
    # Finish-kind fallback labels — only used when a template's own
    # family_label isn't set; per-listing family_label always wins.
    "finish_label_non_holo": "Non-Holo",
    "finish_label_reverse_holo": "Reverse Holo",
    "finish_label_poke_ball": "Poké Ball",
    "finish_label_master_ball": "Master Ball",
    "finish_label_ultra_rare": "Ultra Rare",
}


def _finish_label(finish_kind: str | None, theme: dict) -> str | None:
    if not finish_kind:
        return None
    return theme.get(f"finish_label_{finish_kind}", finish_kind)


def _load_theme(cur, theme_key: str = "default") -> dict:
    cur.execute("SELECT key, value FROM description_theme_settings WHERE theme_key = %s", (theme_key,))
    overrides = {r["key"]: r["value"] for r in cur.fetchall()}
    return {**DEFAULT_THEME, **overrides}


def list_theme_settings(theme_key: str = "default") -> list[dict]:
    with db_cursor() as cur:
        cur.execute(
            "SELECT key, value, label, category, theme_key, updated_at FROM description_theme_settings "
            "WHERE theme_key = %s ORDER BY category, label",
            (theme_key,),
        )
        return cur.fetchall()


def update_theme_setting(theme_key: str, key: str, value: str) -> dict:
    with db_cursor() as cur:
        cur.execute(
            "UPDATE description_theme_settings SET value = %s, updated_at = now() "
            "WHERE theme_key = %s AND key = %s RETURNING key, value, label, category, theme_key, updated_at",
            (value, theme_key, key),
        )
        row = cur.fetchone()
    if row is None:
        return {"key": key, "theme_key": theme_key, "error": "no such theme setting"}
    return row


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
# Block markup — Deep Sea theme (Fei, 8/08). Dark-theme sanitizer
# hardening (critical: a stripped background on a dark theme = light text
# on eBay's default white = invisible): every colored container is a
# <table>/<td> — never a <div> — carrying BOTH the legacy bgcolor
# attribute AND inline style="background:...". bgcolor is not a valid div
# attribute, which is the whole reason these are tables; sanitizers
# reportedly respect bgcolor more reliably than CSS background. Milestone
# 4 (push ONE real listing, view live, confirm no invisible regions) is
# still the gate before wider rollout — see plan doc roadmap.
# ══════════════════════════════════════════════════════════════════════════════

def _two_col_rows(cells: list[str], cell_padding: str = "6px") -> str:
    """Wraps each inner-content string in a 50%-width <td> and chunks them
    2-per-row, padding a trailing odd cell so the row doesn't collapse
    lopsided. Shared by family tiles and era list rows — same wrapping
    behavior, different cell content. The <td>/grid wrapping stays
    Python's job even when the cell's INNER content comes from a custom
    item_template — so template authors only ever write "what one tile
    looks like", never grid mechanics."""
    wrapped = [f'<td width="50%" valign="top" style="padding:{cell_padding};">{c}</td>' for c in cells]
    rows = []
    for i in range(0, len(wrapped), 2):
        pair = wrapped[i:i + 2]
        if len(pair) == 1:
            pair.append('<td width="50%">&nbsp;</td>')
        rows.append(f"<tr>{''.join(pair)}</tr>")
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="border-collapse:collapse;">{"".join(rows)}</table>')


def _substitute_item_placeholders(html: str, label: str, url: str | None, image_url: str | None,
                                   description: str | None = None) -> str:
    """Substitutes {{item_label}}/{{item_url}}/{{item_image_url}}/
    {{item_description}} into a module's item_template_html/
    item_template_current_html. Pure string function — the module row
    (and its template HTML) is already in hand by the time this is
    called; no DB lookup here (that used to be a separate query per item
    per render, folded away in migration 029's redesign)."""
    values = {"label": label or "", "url": url or "", "image_url": image_url or "",
              "description": description or ""}
    return _ITEM_PLACEHOLDER_PATTERN.sub(lambda m: values.get(m.group(1), ""), html)


def _resolve_item_html(module: dict, current: bool, label: str, url: str | None, image_url: str | None,
                        default_render, description: str | None = None) -> str:
    """One item's inner HTML: the module's item_template_current_html (if
    `current` and set), else its item_template_html (if set), else
    default_render() (the built-in Python markup) — NULL/unset falls
    through to the next tier rather than being an error, so a module
    never needs both columns filled in just to exist."""
    if current and module.get("item_template_current_html"):
        return _substitute_item_placeholders(
            module["item_template_current_html"], label, url, image_url, description)
    if module.get("item_template_html"):
        return _substitute_item_placeholders(module["item_template_html"], label, url, image_url, description)
    return default_render()


def _module_text(module: dict, field: str, template: dict, theme: dict, simple: dict) -> str | None:
    """A repeater module's title/subtitle override, if set — substituted
    against the CURRENT template (not the loop's items) so e.g.
    {{finish_label}} still works in a custom era_siblings title, which
    varies per finish_kind on the same hub set in the built-in default.
    Returns None (caller falls back to the built-in default) when the
    module doesn't override this field."""
    value = module.get(field)
    if not value:
        return None
    ctx = {**simple, "finish_label": _finish_label(template.get("finish_kind"), theme) or ""}
    return TOKEN_PATTERN.sub(lambda m: ctx.get(m.group(1), ""), value)


def _wrap_repeater(cells: list[str], layout: str, title: str, subtitle: str | None, theme: dict,
                    cell_padding: str = "6px") -> str:
    if layout == "chips":
        return _chip_row_html(cells)
    return _nav_block_html(title, cells, theme, subtitle=subtitle, cell_padding=cell_padding)


def _nav_cell_html(label: str, url: str | None, image_url: str | None, theme: dict,
                    highlighted: bool = False) -> str:
    """Family-strip tile: dark panel, uppercase micro-label above the
    image, "View listing" / "Viewing this" pill button below it. Falls
    back to a plain block placeholder when nav_image_url isn't set yet."""
    d = theme
    # Height follows width at the card-art aspect ratio (76:106, the
    # original Deep Sea mockup's proportions) rather than being a second
    # independently-tweakable number — one knob, no risk of a mismatched
    # width/height stretching the image.
    w = int(d["nav_tile_width"])
    h = round(w * 106 / 76)
    img = (f'<img src="{image_url}" alt="{label}" style="width:{w}px;height:{h}px;'
           f'object-fit:cover;border-radius:5px;display:block;margin:0 auto 10px;">'
           if image_url else
           f'<div style="width:{w}px;height:{h}px;border-radius:5px;margin:0 auto 10px;'
           f'background:{d["color_border"]};"></div>')

    if highlighted:
        label_color = d["color_cyan"]
        button = (f'<span style="display:inline-block;font-size:11px;font-weight:bold;'
                  f'color:{d["color_bg"]};background:#fff;border-radius:7px;padding:7px 14px;'
                  f'font-family:sans-serif;">{d["text_viewing_this"]}</span>')
        bgcolor = d["color_panel"]
        bg_style = f'background:linear-gradient(155deg,{d["color_panel"]},{d["color_bg"]});'
        border, extra = f'2px solid {d["color_cyan"]}', 'box-shadow:0 0 14px rgba(63,195,232,0.3);'
    else:
        label_color = d["color_text_muted"]
        button = (f'<span style="display:inline-block;font-size:12px;font-weight:bold;'
                  f'color:{d["color_bg"]};background:{d["color_cyan"]};border-radius:7px;padding:7px 16px;'
                  f'font-family:sans-serif;">{d["text_view_listing"]}</span>')
        bgcolor, bg_style = d["color_panel"], f'background:{d["color_panel"]};'
        border, extra = f'1px solid {d["color_border"]}', ''

    inner = (f'<span style="display:block;font-size:10px;color:{label_color};text-transform:uppercase;'
             f'letter-spacing:0.06em;margin-bottom:8px;font-family:sans-serif;">{label}</span>'
             f'{img}{button}')

    tile = (f'<table role="presentation" width="100%" bgcolor="{bgcolor}" cellpadding="0" cellspacing="0" '
            f'style="{bg_style}border:{border};border-radius:11px;{extra}"><tr>'
            f'<td style="padding:14px;text-align:center;">{inner}</td></tr></table>')
    if url:
        tile = f'<a href="{url}" style="display:block;text-decoration:none;">{tile}</a>'
    return tile


def _era_list_cell_html(label: str, url: str | None, theme: dict) -> str:
    """Era-nav row: compact icon + set name + chevron link — a list, not
    big tiles, since an era can have 6-8+ sets where family_nav usually
    only has 2-4."""
    d = theme
    icon = f'<span style="display:inline-block;width:32px;height:45px;border-radius:4px;background:{d["color_border"]};"></span>'
    inner = (f'<table role="presentation" cellpadding="0" cellspacing="0"><tr>'
             f'<td valign="middle" style="padding-right:10px;">{icon}</td>'
             f'<td valign="middle"><span style="display:block;font-size:13px;font-weight:bold;'
             f'color:{d["color_text_light"]};font-family:sans-serif;">{label}</span>'
             f'<span style="display:block;font-size:11px;color:{d["color_cyan"]};font-family:sans-serif;">'
             f'{d["text_era_list_link"]} &rsaquo;</span></td></tr></table>')
    row = (f'<table role="presentation" width="100%" bgcolor="{d["color_panel"]}" cellpadding="0" cellspacing="0" '
           f'style="background:{d["color_panel"]};border:1px solid {d["color_border"]};border-radius:9px;">'
           f'<tr><td style="padding:11px 12px;">{inner}</td></tr></table>')
    if url:
        row = f'<a href="{url}" style="display:block;text-decoration:none;">{row}</a>'
    return row


def _nav_block_html(title: str, cells: list[str], theme: dict, subtitle: str | None = None,
                     cell_padding: str = "6px") -> str:
    sub = (f'<p style="margin:0 0 12px;font-size:12px;color:{theme["color_text_muted"]};'
           f'font-family:sans-serif;">{subtitle}</p>') if subtitle else ""
    return (f'<div style="margin:20px 0;"><p style="margin:0 0 {"4px" if subtitle else "12px"};'
            f'font-size:15px;font-weight:bold;color:#fff;font-family:sans-serif;">{title}</p>'
            f'{sub}{_two_col_rows(cells, cell_padding)}</div>')


def _banner_html(text: str, url: str, theme: dict) -> str:
    d = theme
    return (f'<table role="presentation" width="100%" bgcolor="{d["color_panel"]}" cellpadding="0" cellspacing="0" '
            f'style="background:{d["color_panel"]};border:1px solid {d["color_cyan"]};border-radius:9px;margin:16px 0;">'
            f'<tr><td style="padding:12px;text-align:center;">'
            f'<a href="{url}" style="display:block;text-decoration:none;font-family:sans-serif;'
            f'font-weight:bold;font-size:13px;color:{d["color_cyan"]};">{text} &rarr;</a>'
            f'</td></tr></table>')


def _chip_row_html(chips: list[str]) -> str:
    return f'<div style="margin:16px 0;">{"".join(chips)}</div>'


def _chip_html(label: str, url: str | None, highlighted: bool, theme: dict) -> str:
    d = theme
    text = f'{label} {d["text_youre_here_suffix"]}' if highlighted else label
    common = ("display:inline-block;margin:2px;padding:4px 10px;border-radius:12px;"
              "font-family:sans-serif;font-size:12px;text-decoration:none;")
    style = common + (f'background:{d["color_cyan"]};color:{d["color_bg"]};' if highlighted
                       else f'background:transparent;border:1px solid {d["color_border"]};color:{d["color_text_dim"]};')
    if url:
        return f'<a href="{url}" style="{style}">{text}</a>'
    return f'<span style="{style}">{text}</span>'


# ══════════════════════════════════════════════════════════════════════════════
# Repeat-rule / single-target resolvers — the DATA logic (which listings a
# module shows) for each repeat_rule, decoupled from a module's own
# PRESENTATION (its item_template_html, layout, title/subtitle). Any
# number of differently-named, differently-styled modules can share one
# repeat_rule; this is what replaced the old fixed family_nav/era_nav/
# era_hub_link/era_index singleton functions (migration 029 redesign).
# Registered in _REPEAT_RESOLVERS/_SINGLE_RESOLVERS below, dispatched by
# render_description()'s substitute().
# ══════════════════════════════════════════════════════════════════════════════

def _render_repeater_family(cur, template: dict, theme: dict, module: dict, simple: dict) -> str:
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
        label = s["family_label"] or _finish_label(s["finish_kind"], theme) or "Listing"
        url = None if is_self else _item_url(s["listing_id"])
        image_url = s["nav_image_url"]
        description = s["family_description"]
        cells.append(_resolve_item_html(
            module, is_self, label, url, image_url,
            default_render=lambda label=label, url=url, image_url=image_url, is_self=is_self:
                _nav_cell_html(label, url, image_url, theme, highlighted=is_self),
            description=description,
        ))
    title = _module_text(module, "title", template, theme, simple) or theme["text_family_nav_title"]
    subtitle = _module_text(module, "subtitle", template, theme, simple)
    return _wrap_repeater(cells, module.get("layout") or "grid", title, subtitle, theme, cell_padding="6px")


def _render_repeater_era_siblings(cur, template: dict, theme: dict, module: dict, simple: dict) -> str:
    if not template.get("set_id"):
        return ""
    cur.execute("SELECT * FROM card_sets WHERE id = %s", (template["set_id"],))
    my_set = cur.fetchone()
    if not my_set or not my_set["series"]:
        return ""
    base_set = _era_base_set(cur, my_set["series"])
    if not base_set or base_set["id"] != my_set["id"]:
        return ""  # only renders on the hub itself

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
        label, url = s["name"], _item_url(match["listing_id"])
        image_url = match["nav_image_url"]
        cells.append(_resolve_item_html(
            module, False, label, url, image_url,
            default_render=lambda label=label, url=url, theme=theme:
                _era_list_cell_html(label, url, theme),
        ))
    if not cells:
        return ""

    finish_label = _finish_label(template.get("finish_kind"), theme)
    default_title = f"Other {finish_label} sets" if finish_label else f"Other {my_set['series']} era sets"
    title = _module_text(module, "title", template, theme, simple) or default_title
    subtitle = _module_text(module, "subtitle", template, theme, simple)
    if subtitle is None:
        subtitle = theme["text_era_nav_subtitle"]
    return _wrap_repeater(cells, module.get("layout") or "grid", title, subtitle, theme, cell_padding="5px")


def _render_repeater_era_index(cur, template: dict, theme: dict, module: dict, simple: dict) -> str:
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
        url = None if is_self else _item_url(match["listing_id"])
        chips.append(_resolve_item_html(
            module, is_self, series, url, None,
            default_render=lambda series=series, url=url, is_self=is_self:
                _chip_html(series, url, is_self, theme),
        ))
    if not chips:
        return ""
    layout = module.get("layout") or "chips"
    title = _module_text(module, "title", template, theme, simple) or "Other eras"
    subtitle = _module_text(module, "subtitle", template, theme, simple)
    return _wrap_repeater(chips, layout, title, subtitle, theme, cell_padding="6px")


def _render_single_era_hub(cur, template: dict, theme: dict, module: dict) -> str:
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
    text = f"Shop all {series} era sets"
    url = _item_url(hub_template["listing_id"])
    if module.get("item_template_html"):
        return _substitute_item_placeholders(module["item_template_html"], text, url, None)
    return _banner_html(text, url, theme)


def _render_single_self(cur, template: dict, theme: dict, module: dict) -> str:
    # The one rule with no Python-hardcoded look at all — unlike the other
    # 3 built-ins, a bare {{some_key}} standalone module was never a thing
    # before migration 029, so there's no "default" to preserve here.
    if not module.get("item_template_html"):
        return ""
    label = template.get("family_label") or _finish_label(template.get("finish_kind"), theme) or "Listing"
    return _substitute_item_placeholders(
        module["item_template_html"], label, None, template.get("nav_image_url"),
        description=template.get("family_description"))


_REPEAT_RESOLVERS = {
    "family": _render_repeater_family,
    "era_siblings": _render_repeater_era_siblings,
    "era_index": _render_repeater_era_index,
}

_SINGLE_RESOLVERS = {
    "era_hub": _render_single_era_hub,
    "self": _render_single_self,
}


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


def _load_module(cur, key: str) -> dict | None:
    cur.execute("SELECT * FROM description_sections WHERE key = %s", (key,))
    return cur.fetchone()


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
    template['description_html']) against current DB state. A template
    with no tokens renders unchanged — the safe default for every
    template Fei hasn't opted into nav for.

    Token resolution order: (1) the two plain-text tokens (set_name/
    series_name) — pure Python, not a description_sections row; (2)
    everything else is a module lookup by key (description_sections,
    migration 029's kind='static'/'repeater'/'single' shapes) —
    'static' substitutes recursively (its own html can reference other
    modules, e.g. {{family_nav}} inside a saved layout's html — guarded
    against a module referencing itself, directly or through a cycle, by
    `rendering`); 'repeater'/'single' dispatch to a rule resolver keyed
    by repeat_rule. A key matching no module still renders as empty
    string, not an error — typos/stray {{...}} in freeform HTML
    shouldn't break the page. The 4 original built-in blocks
    (family_nav/era_nav/era_hub_link/era_index) are just modules seeded
    under those exact names by backfill_description_modules() — no
    special-casing left for them here."""
    source = source_html if source_html is not None else (template.get("description_html") or "")
    if not source:
        return source

    theme = _load_theme(cur, template.get("theme_key") or "default")
    simple = _render_simple_tokens(cur, template)
    cache = {}
    rendering = set()

    def substitute(match):
        token = match.group(1)
        if token in simple:
            return simple[token]
        if token in cache:
            return cache[token]
        if token in rendering:
            return ""  # cycle guard: a static module referencing itself/a loop of modules
        module = _load_module(cur, token)
        if module is None:
            return ""
        rendering.add(token)
        try:
            kind = module.get("kind")
            if kind == "static":
                result = TOKEN_PATTERN.sub(substitute, module.get("html") or "")
            elif kind == "repeater":
                resolver = _REPEAT_RESOLVERS.get(module.get("repeat_rule"))
                result = resolver(cur, template, theme, module, simple) if resolver else ""
            elif kind == "single":
                resolver = _SINGLE_RESOLVERS.get(module.get("repeat_rule"))
                result = resolver(cur, template, theme, module) if resolver else ""
            else:
                result = ""
        finally:
            rendering.discard(token)
        cache[token] = result
        return result

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
# One-time (re-runnable) description_sections -> module conversion
# (migration 029, Fei 8/09's "full customizable description module
# builder" redesign — replaces the reserved-key/{{token:modifier}} special
# cases with one coherent per-module model). Seeds the 4 built-in modules
# under the SAME token names the renderer has always recognized
# (family_nav/era_nav/era_hub_link/era_index), so every listing's
# description_html renders byte-identical afterward — nothing needs
# re-authoring. Then folds any leftover kind='item_template' rows (the 5
# reserved sub-keys, if they exist, or freeform standalone keys like the
# _example rows) into the new shape.
# ══════════════════════════════════════════════════════════════════════════════

_BUILTIN_MODULES = [
    # key, label, kind, repeat_rule, layout, sort_order
    ("family_nav", "Family strip (finish variants of this set)", "repeater", "family", "grid", 1),
    ("era_nav", "Era navigation (other sets in this era, hub only)", "repeater", "era_siblings", "grid", 2),
    ("era_hub_link", "Era hub link (banner, non-hub listings only)", "single", "era_hub", None, 3),
    ("era_index", "Era index (every era's hub set, chip row)", "repeater", "era_index", "chips", 4),
]

# Old reserved item-template keys -> which built-in module's
# item_template_*/item_template_current_* column they fold into.
_RESERVED_ITEM_TEMPLATE_MAP = {
    "family_tile": ("family_nav", "item_template_html"),
    "family_tile_current": ("family_nav", "item_template_current_html"),
    "era_row": ("era_nav", "item_template_html"),
    "era_chip": ("era_index", "item_template_html"),
    "era_chip_current": ("era_index", "item_template_current_html"),
}


def backfill_description_modules() -> dict:
    """Seeds the 4 built-in modules and converts leftover
    kind='item_template' rows to the new repeater/single shape. Idempotent
    — safe to re-run. NOT wired to a main.py flag (one-off, same as
    backfill_nav_images originally was) — run directly when deploying
    migration 029."""
    with db_cursor() as cur:
        for key, label, kind, repeat_rule, layout, sort_order in _BUILTIN_MODULES:
            cur.execute(
                """
                INSERT INTO description_sections (key, label, kind, repeat_rule, layout, sort_order)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (key) DO NOTHING
                """,
                (key, label, kind, repeat_rule, layout, sort_order),
            )

        cur.execute("SELECT id, key, html FROM description_sections WHERE kind = 'item_template'")
        leftover = cur.fetchall()

        folded, converted = [], []
        for row in leftover:
            if row["key"] in _RESERVED_ITEM_TEMPLATE_MAP:
                target_key, column = _RESERVED_ITEM_TEMPLATE_MAP[row["key"]]
                cur.execute(
                    f"UPDATE description_sections SET {column} = %s WHERE key = %s",
                    (row["html"], target_key),
                )
                cur.execute("DELETE FROM description_sections WHERE id = %s", (row["id"],))
                folded.append(row["key"])
            else:
                cur.execute(
                    """
                    UPDATE description_sections
                    SET kind = 'single', repeat_rule = 'self', item_template_html = html, html = NULL
                    WHERE id = %s
                    """,
                    (row["id"],),
                )
                converted.append(row["key"])

    return {"builtins_seeded": [m[0] for m in _BUILTIN_MODULES], "folded_into_builtins": folded,
            "converted_to_standalone": converted}


# ══════════════════════════════════════════════════════════════════════════════
# Reusable module library (migration 021, extended by migration 029) —
# supersedes the old hardcoded DESCRIPTION_PRESETS constant. kind='static'
# rows are plain reusable HTML (formerly split into 'layout'/'section' —
# that was purely an authoring-time UX distinction in the old textarea-
# based editor, not a rendering one, so it collapsed into one kind);
# kind='repeater'/'single' rows are the module-builder shapes, see the
# "Token renderers" section below. All may contain {{tokens}}, substituted
# by the same render_description() pass — no separate rendering path for
# library content.
# ══════════════════════════════════════════════════════════════════════════════

_FULL_SECTION_COLUMNS = ("id, key, label, html, kind, sort_order, repeat_rule, layout, "
                         "item_template_html, item_template_current_html, title, subtitle, updated_at")

_REPEATER_RULES = {"family", "era_siblings", "era_index"}
_SINGLE_RULES = {"self", "era_hub"}
_LAYOUTS = {"grid", "chips"}


def _validate_module_fields(kind: str, repeat_rule: str | None, layout: str | None, html: str | None) -> None:
    """Application-level guard for (kind, repeat_rule, layout) combinations
    the CHECK constraint alone can't express. kind='item_template' is
    grandfathered (legacy rows only — the UI stopped producing that kind
    after migration 029) and skips these checks entirely."""
    if kind == "item_template":
        return
    if kind == "static":
        if not html:
            raise ValueError("a 'static' module needs html")
        if repeat_rule or layout:
            raise ValueError("'static' modules don't use repeat_rule/layout")
    elif kind == "repeater":
        if repeat_rule not in _REPEATER_RULES:
            raise ValueError(f"repeat_rule for a 'repeater' module must be one of {sorted(_REPEATER_RULES)}")
        if layout and layout not in _LAYOUTS:
            raise ValueError(f"layout must be one of {sorted(_LAYOUTS)}")
    elif kind == "single":
        if repeat_rule not in _SINGLE_RULES:
            raise ValueError(f"repeat_rule for a 'single' module must be one of {sorted(_SINGLE_RULES)}")
        if layout:
            raise ValueError("'single' modules don't use layout")
    else:
        raise ValueError(f"unknown kind {kind!r}")


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
    """Full rows (module-builder columns included, migration 029) for the
    module library UI — list_description_sections()'s trimmed shape is
    for the dropdown, this is for CRUD."""
    with db_cursor() as cur:
        cur.execute(f"SELECT {_FULL_SECTION_COLUMNS} FROM description_sections ORDER BY sort_order, label")
        return cur.fetchall()


def create_description_section(key: str, label: str, kind: str = "static", html: str | None = None,
                                sort_order: int = 0, repeat_rule: str | None = None, layout: str | None = None,
                                item_template_html: str | None = None,
                                item_template_current_html: str | None = None,
                                title: str | None = None, subtitle: str | None = None) -> dict:
    _validate_module_fields(kind, repeat_rule, layout, html)
    with db_cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO description_sections
                (key, label, html, kind, sort_order, repeat_rule, layout,
                 item_template_html, item_template_current_html, title, subtitle)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING {_FULL_SECTION_COLUMNS}
            """,
            (key, label, html, kind, sort_order, repeat_rule, layout,
             item_template_html, item_template_current_html, title, subtitle),
        )
        return cur.fetchone()


_SECTION_FIELDS = ("key", "label", "html", "kind", "sort_order", "repeat_rule", "layout",
                    "item_template_html", "item_template_current_html", "title", "subtitle")


def update_description_section(section_id: str, **fields) -> dict:
    """Partial update — same pattern as set_manual_listing_metadata():
    only columns present in fields are touched. Fetches the existing row
    first so validation sees the EFFECTIVE (kind, repeat_rule, layout,
    html) after merging the partial update, not just whichever fields
    happened to be part of this one call."""
    cols = [f for f in _SECTION_FIELDS if f in fields]
    if not cols:
        return {"id": section_id, "updated": []}
    with db_cursor() as cur:
        cur.execute(
            "SELECT kind, repeat_rule, layout, html FROM description_sections WHERE id = %s",
            (section_id,),
        )
        existing = cur.fetchone()
        if existing is None:
            return {"id": section_id, "error": "no such section"}
        merged = {**existing, **{c: fields[c] for c in cols}}
        _validate_module_fields(merged["kind"], merged["repeat_rule"], merged["layout"], merged["html"])

        set_clause = ", ".join(f"{c} = %s" for c in cols)
        values = [fields[c] for c in cols]
        cur.execute(
            f"UPDATE description_sections SET {set_clause}, updated_at = now() "
            f"WHERE id = %s RETURNING {_FULL_SECTION_COLUMNS}",
            values + [section_id],
        )
        return cur.fetchone()


def delete_description_section(section_id: str) -> dict:
    with db_cursor() as cur:
        cur.execute("DELETE FROM description_sections WHERE id = %s RETURNING id", (section_id,))
        row = cur.fetchone()
    return {"id": section_id, "deleted": row is not None}
