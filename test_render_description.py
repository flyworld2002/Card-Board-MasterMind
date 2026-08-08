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

        print("PASS — no exceptions, unchanged-template invariant held. Rolling back seed data now.")
    finally:
        conn.rollback()
        cur.close()
        conn.close()


if __name__ == "__main__":
    main()
