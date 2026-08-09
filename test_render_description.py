"""
test_render_description.py — standalone diagnostic for
importer/ebay_descriptions.py's render_description(): seeds a fake era
(one hub set + two spoke sets, sharing a fake series) and a small roster
of listing_templates rows entirely inside one DB transaction, renders
every token against them, prints the output for inspection, then rolls
back — nothing written stays written, no eBay call is ever made.

Deliberately a standalone script, not wired into main.py — a one-off
build-time check for the description-nav plan (milestone 2), same
pattern as test_delete_variation.py.

USAGE:
    python3 test_render_description.py

Requires migration 020 already applied (listing_templates.set_id /
finish_kind / family_label / nav_rank / is_set_primary / show_in_nav /
nav_image_url columns) — fails loudly and rolls back if it isn't.

Also requires migration 029 applied AND backfill_description_modules()
already run (once, committed — see importer/ebay_descriptions.py) so the
4 built-in modules (family_nav/era_nav/era_hub_link/era_index) exist as
real description_sections rows; this script's own transaction only seeds
listing_templates/card_sets fixtures plus a few one-off module-override
rows per test case, not the built-ins themselves.
"""

from db.connection import get_connection
from importer.ebay_descriptions import render_description

SERIES = "__TestEra__"


def seed(cur):
    cur.execute("SELECT id FROM card_games LIMIT 1")
    game_id = cur.fetchone()["id"]

    def make_set(name):
        cur.execute(
            """
            INSERT INTO card_sets (game_id, name, series, set_code)
            VALUES (%s, %s, %s, %s) RETURNING id
            """,
            (game_id, name, SERIES, f"__TEST_{name}__"),
        )
        return cur.fetchone()["id"]

    hub_set_id = make_set(SERIES)          # base set: name == series
    spoke_set_a_id = make_set("__TestSetA__")
    spoke_set_b_id = make_set("__TestSetB__")

    def make_template(name, set_id, finish_kind, listing_id, is_set_primary=False,
                       show_in_nav=True, family_label=None, nav_rank=None,
                       description_html=None):
        cur.execute(
            """
            INSERT INTO listing_templates
                (name, set_id, finish_kind, listing_id, is_set_primary,
                 show_in_nav, family_label, nav_rank, description_html)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *
            """,
            (name, set_id, finish_kind, listing_id, is_set_primary,
             show_in_nav, family_label, nav_rank, description_html),
        )
        return cur.fetchone()

    hub_common = make_template("hub common", hub_set_id, "non_holo", "TEST-HUB-COMMON",
                                is_set_primary=True, family_label="Non-Holo", nav_rank=1,
                                description_html="<h1>Hub</h1>{{era_nav}}{{era_index}}")
    hub_rh = make_template("hub RH", hub_set_id, "reverse_holo", "TEST-HUB-RH",
                            family_label="Reverse Holo", nav_rank=2,
                            description_html="{{family_nav}}")

    spoke_a_common = make_template("spoke A common", spoke_set_a_id, "non_holo", "TEST-A-COMMON",
                                    is_set_primary=True,
                                    description_html="{{set_name}} / {{series_name}}{{era_hub_link}}")
    make_template("spoke A RH", spoke_set_a_id, "reverse_holo", "TEST-A-RH")

    # Spoke B has NO reverse_holo template at all — exercises the
    # "no finish match -> fall back to is_set_primary" path.
    spoke_b_common = make_template("spoke B common", spoke_set_b_id, "non_holo", "TEST-B-COMMON",
                                    is_set_primary=True)

    return {
        "hub_common": hub_common, "hub_rh": hub_rh,
        "spoke_a_common": spoke_a_common, "spoke_b_common": spoke_b_common,
    }


def main():
    conn = get_connection()
    cur = conn.cursor()
    try:
        rows = seed(cur)

        print("=== hub_common: description_html has {{era_nav}} + {{era_index}} ===")
        print(render_description(rows["hub_common"], cur))
        print()

        print("=== hub_rh: {{family_nav}} (sibling = hub_common, same set) ===")
        print(render_description(rows["hub_rh"], cur))
        print()

        print("=== spoke_a_common: {{set_name}} / {{series_name}} + {{era_hub_link}} ===")
        print(render_description(rows["spoke_a_common"], cur))
        print()

        print("=== spoke_b_common: era_hub_link's finish (non_holo) DOES match the hub directly ===")
        print(render_description(rows["spoke_b_common"], cur,
                                  source_html="{{era_hub_link}}"))
        print()

        print("=== template with no tokens renders unchanged ===")
        unchanged = {**rows["spoke_b_common"], "description_html": "<p>plain, no tokens</p>"}
        out = render_description(unchanged, cur)
        assert out == "<p>plain, no tokens</p>", f"expected unchanged, got: {out!r}"
        print(out)
        print()

        print("=== hub_rh: {{family_nav}} with the family_nav module's own item_template_html override ===")
        # Migration 029 redesign: overriding a repeater's per-item look
        # now means setting item_template_html directly on the module row
        # itself (here, temporarily on the live 'family_nav' row, within
        # this script's own rolled-back transaction) — no separate
        # reserved-key row, no {{token:modifier}} syntax (dropped earlier
        # this session). item_template_current_html left unset
        # deliberately, to also exercise the fallback path (current item
        # falls back to the plain item_template_html, not the Python
        # default).
        cur.execute(
            """
            UPDATE description_sections
            SET item_template_html = '<div class="custom-tile">{{item_label}} | {{item_url}} | {{item_image_url}}</div>'
            WHERE key = 'family_nav'
            """
        )
        custom_out = render_description(rows["hub_rh"], cur, source_html="{{family_nav}}")
        print(custom_out)
        assert 'class="custom-tile"' in custom_out, "custom item template did not apply"
        assert "Non-Holo | https://www.ebay.com/itm/TEST-HUB-COMMON |" in custom_out, \
            "sibling (non-current) tile did not substitute label/url correctly"
        assert "Reverse Holo |  |" in custom_out, \
            "current tile (no item_template_current_html) did not fall back to item_template_html correctly"
        assert "View listing" not in custom_out, "default Python tile markup leaked in despite override"
        print()

        print("=== hub_common: {{era_index}} with era_index's item_template_html/current override ===")
        # Covers the OTHER loop that can include "myself" (family isn't
        # the only one) — era_index's current chip is the current
        # template's own series, exercised here since hub_common's set
        # IS its series' hub.
        cur.execute(
            """
            UPDATE description_sections
            SET item_template_html = '<span class="chip">{{item_label}}</span>',
                item_template_current_html = '<span class="chip-current">{{item_label}}</span>'
            WHERE key = 'era_index'
            """
        )
        era_index_out = render_description(rows["hub_common"], cur, source_html="{{era_index}}")
        print(era_index_out)
        assert '<span class="chip-current">__TestEra__</span>' in era_index_out, \
            "era_index current chip (my own era) did not use item_template_current_html"
        assert 'class="chip">' in era_index_out, "era_index non-current chip did not use item_template_html"
        assert "(you're here)" not in era_index_out, "default Python chip markup leaked in despite override"
        print()

        print("=== spoke_a_common: {{__standalone_test__}} — a 'single'/'self' module, no nav loop ===")
        # A single/repeat_rule=self module renders using the CURRENT
        # template's own label/image/description and an empty url
        # (self-link) — the module-builder replacement for what migration
        # 026's "_example" rows demonstrated and what the ad-hoc
        # standalone-token fallback did before this redesign.
        # spoke_a_common has no family_label set, so this also exercises
        # the finish_kind -> theme fallback label path.
        cur.execute(
            """
            INSERT INTO description_sections (key, label, kind, repeat_rule, item_template_html)
            VALUES ('__standalone_test__', 'standalone test', 'single', 'self',
                    '<div class="standalone">{{item_label}} | {{item_url}} | {{item_image_url}} | {{item_description}}</div>')
            """
        )
        standalone_out = render_description(rows["spoke_a_common"], cur, source_html="{{__standalone_test__}}")
        print(standalone_out)
        assert 'class="standalone"' in standalone_out, "single/self module did not resolve"
        assert "Non-Holo |  |  | " in standalone_out, \
            "single/self module should use the template's own finish-label fallback, empty url/image/description"
        print()

        print("=== static module referenced by {{key}} recursively substitutes nested tokens ===")
        # New in the module-builder redesign: a 'static' module can be
        # REFERENCED (not just pasted) — its own html goes through the
        # same substitution pass as the outer description, so nested
        # {{tokens}} inside it resolve too.
        cur.execute(
            """
            INSERT INTO description_sections (key, label, html, kind)
            VALUES ('__static_test__', 'static test', '<h2>{{set_name}}</h2>{{era_hub_link}}', 'static')
            """
        )
        static_out = render_description(rows["spoke_a_common"], cur, source_html="{{__static_test__}}")
        inline_out = render_description(rows["spoke_a_common"], cur, source_html="<h2>{{set_name}}</h2>{{era_hub_link}}")
        assert static_out == inline_out, \
            "static module reference did not recursively substitute nested tokens the same as writing them inline"
        print(static_out)
        print()

        print("=== cycle guard: a static module referencing itself renders empty for the inner occurrence ===")
        cur.execute(
            """
            INSERT INTO description_sections (key, label, html, kind)
            VALUES ('__cycle_test__', 'cycle test', 'before[{{__cycle_test__}}]after', 'static')
            """
        )
        cycle_out = render_description(rows["spoke_a_common"], cur, source_html="{{__cycle_test__}}")
        assert cycle_out == "before[]after", f"expected cycle guard to render the inner occurrence empty, got: {cycle_out!r}"
        print(cycle_out)
        print()

        print("=== unknown token (no matching module) still renders empty, not an error ===")
        empty_out = render_description(rows["spoke_a_common"], cur, source_html="before[{{__no_such_key__}}]after")
        assert empty_out == "before[]after", f"expected empty substitution, got: {empty_out!r}"
        print(empty_out)
        print()

        print("PASS — no exceptions, unchanged-template invariant held, item_template_html override verified "
              "(family + era_index current-chip), single/self module verified, static module reference + "
              "recursion + cycle guard verified. Rolling back seed data now.")
    finally:
        conn.rollback()
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
