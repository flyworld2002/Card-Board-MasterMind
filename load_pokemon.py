#!/usr/bin/env python3
"""
load_pokemon.py — Populate the pokemon reference tables from pokeapi.co

Usage:
    python3 load_pokemon.py              # load all Pokémon (no moves)
    python3 load_pokemon.py --moves      # include moves (~150k rows, slow)
    python3 load_pokemon.py --limit 151  # only first 151 (for testing)
    python3 load_pokemon.py --start 152  # resume from a specific ID

No API key required. Rate-limits itself to be polite to the free API.
Takes ~5-10 minutes for all 1025 Pokémon without moves.
"""

import argparse
import time
import re
import sys
import requests
from db.connection import db_cursor

POKEAPI = "https://pokeapi.co/api/v2"
DELAY   = 0.3   # seconds between API calls (polite rate limiting)


# ── HTTP helper ───────────────────────────────────────────────────────────────

def fetch(url: str, retries: int = 3) -> dict:
    for attempt in range(retries):
        try:
            r = requests.get(url, timeout=15)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            if attempt < retries - 1:
                print(f"  ⚠️  Retry {attempt + 1}: {e}")
                time.sleep(2 ** attempt)
            else:
                raise
    return {}


def title_case(s: str) -> str:
    """Convert API slug to title case: 'mr-mime' → 'Mr. Mime'"""
    # Special cases
    SPECIAL = {
        "mr-mime": "Mr. Mime", "mr-rime": "Mr. Rime",
        "mime-jr": "Mime Jr.", "ho-oh": "Ho-Oh",
        "porygon-z": "Porygon-Z", "type-null": "Type: Null",
        "jangmo-o": "Jangmo-o", "hakamo-o": "Hakamo-o",
        "kommo-o": "Kommo-o", "wo-chien": "Wo-Chien",
        "chien-pao": "Chien-Pao", "ting-lu": "Ting-Lu",
        "chi-yu": "Chi-Yu",
    }
    if s in SPECIAL:
        return SPECIAL[s]
    return " ".join(word.capitalize() for word in s.replace("-", " ").split())


def extract_en(entries: list, key: str = "name") -> str | None:
    """Extract the English entry from a list of language-keyed objects."""
    for e in entries:
        if e.get("language", {}).get("name") == "en":
            return e.get(key) or e.get("flavor_text") or e.get("name")
    return None


def extract_id_from_url(url: str) -> int | None:
    """Extract the numeric ID from a PokeAPI resource URL."""
    m = re.search(r'/(\d+)/?$', url)
    return int(m.group(1)) if m else None


# ── Load one Pokémon ──────────────────────────────────────────────────────────

def load_pokemon(pokemon_id: int, include_moves: bool = False):
    # ── Fetch both endpoints ──────────────────────────────────────────────────
    poke    = fetch(f"{POKEAPI}/pokemon/{pokemon_id}")
    time.sleep(DELAY)
    species = fetch(f"{POKEAPI}/pokemon-species/{pokemon_id}")
    time.sleep(DELAY)

    name = title_case(poke["name"])
    slug = poke["name"]

    # ── Sprites ───────────────────────────────────────────────────────────────
    sprites = poke.get("sprites", {})
    other   = sprites.get("other", {})
    sprite_official    = (other.get("official-artwork", {}) or {}).get("front_default")
    sprite_front       = sprites.get("front_default")
    sprite_front_shiny = sprites.get("front_shiny")

    # ── Generation ───────────────────────────────────────────────────────────
    gen_url  = (species.get("generation") or {}).get("url", "")
    gen_id   = extract_id_from_url(gen_url)

    # ── Genus (English) ───────────────────────────────────────────────────────
    genus = None
    for g in (species.get("genera") or []):
        if (g.get("language") or {}).get("name") == "en":
            genus = g.get("genus")
            break

    # ── Evolution chain ID ────────────────────────────────────────────────────
    evo_url   = (species.get("evolution_chain") or {}).get("url", "")
    evo_chain = extract_id_from_url(evo_url)

    # ── Base stats ────────────────────────────────────────────────────────────
    stats = {s["stat"]["name"]: s["base_stat"] for s in poke.get("stats", [])}

    with db_cursor() as cur:
        # ── 1. Upsert core pokemon record ────────────────────────────────────
        cur.execute("""
            INSERT INTO pokemon (
                id, name, slug, genus, color, shape, habitat, generation,
                is_legendary, is_mythical, is_baby,
                height_dm, weight_hg, base_experience, capture_rate,
                base_happiness, hatch_counter, gender_rate, evolution_chain_id,
                base_hp, base_attack, base_defense,
                base_sp_atk, base_sp_def, base_speed,
                sprite_official, sprite_front, sprite_front_shiny
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s,
                %s, %s, %s
            )
            ON CONFLICT (id) DO UPDATE SET
                name             = EXCLUDED.name,
                genus            = EXCLUDED.genus,
                color            = EXCLUDED.color,
                shape            = EXCLUDED.shape,
                habitat          = EXCLUDED.habitat,
                generation       = EXCLUDED.generation,
                is_legendary     = EXCLUDED.is_legendary,
                is_mythical      = EXCLUDED.is_mythical,
                is_baby          = EXCLUDED.is_baby,
                height_dm        = EXCLUDED.height_dm,
                weight_hg        = EXCLUDED.weight_hg,
                base_experience  = EXCLUDED.base_experience,
                capture_rate     = EXCLUDED.capture_rate,
                base_happiness   = EXCLUDED.base_happiness,
                hatch_counter    = EXCLUDED.hatch_counter,
                gender_rate      = EXCLUDED.gender_rate,
                evolution_chain_id = EXCLUDED.evolution_chain_id,
                base_hp          = EXCLUDED.base_hp,
                base_attack      = EXCLUDED.base_attack,
                base_defense     = EXCLUDED.base_defense,
                base_sp_atk      = EXCLUDED.base_sp_atk,
                base_sp_def      = EXCLUDED.base_sp_def,
                base_speed       = EXCLUDED.base_speed,
                sprite_official  = EXCLUDED.sprite_official,
                sprite_front     = EXCLUDED.sprite_front,
                sprite_front_shiny = EXCLUDED.sprite_front_shiny
        """, (
            pokemon_id, name, slug,
            genus,
            (species.get("color") or {}).get("name"),
            (species.get("shape") or {}).get("name"),
            (species.get("habitat") or {}).get("name"),
            gen_id,
            species.get("is_legendary", False),
            species.get("is_mythical", False),
            species.get("is_baby", False),
            poke.get("height"), poke.get("weight"),
            poke.get("base_experience"),
            species.get("capture_rate"),
            species.get("base_happiness"),
            species.get("hatch_counter"),
            species.get("gender_rate"),
            evo_chain,
            stats.get("hp"), stats.get("attack"), stats.get("defense"),
            stats.get("special-attack"), stats.get("special-defense"),
            stats.get("speed"),
            sprite_official, sprite_front, sprite_front_shiny,
        ))

        # ── 2. Types ─────────────────────────────────────────────────────────
        cur.execute("DELETE FROM pokemon_types WHERE pokemon_id = %s", (pokemon_id,))
        for t in poke.get("types", []):
            cur.execute("""
                INSERT INTO pokemon_types (pokemon_id, slot, type_name)
                VALUES (%s, %s, %s)
                ON CONFLICT DO NOTHING
            """, (pokemon_id, t["slot"], t["type"]["name"]))

        # ── 3. Abilities ──────────────────────────────────────────────────────
        cur.execute("DELETE FROM pokemon_abilities WHERE pokemon_id = %s", (pokemon_id,))
        for a in poke.get("abilities", []):
            cur.execute("""
                INSERT INTO pokemon_abilities (pokemon_id, slot, ability_name, is_hidden)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT DO NOTHING
            """, (pokemon_id, a["slot"], a["ability"]["name"], a["is_hidden"]))

        # ── 4. Egg groups ─────────────────────────────────────────────────────
        cur.execute("DELETE FROM pokemon_egg_groups WHERE pokemon_id = %s", (pokemon_id,))
        for eg in (species.get("egg_groups") or []):
            cur.execute("""
                INSERT INTO pokemon_egg_groups (pokemon_id, egg_group_name)
                VALUES (%s, %s)
                ON CONFLICT DO NOTHING
            """, (pokemon_id, eg["name"]))

        # ── 5. Multi-language names ───────────────────────────────────────────
        cur.execute("DELETE FROM pokemon_names WHERE pokemon_id = %s", (pokemon_id,))
        for n in (species.get("names") or []):
            lang = (n.get("language") or {}).get("name")
            if lang:
                cur.execute("""
                    INSERT INTO pokemon_names (pokemon_id, language, name)
                    VALUES (%s, %s, %s)
                    ON CONFLICT DO NOTHING
                """, (pokemon_id, lang, n["name"]))

        # ── 6. Flavor texts (English only by default to keep size manageable)
        cur.execute("DELETE FROM pokemon_flavor_texts WHERE pokemon_id = %s", (pokemon_id,))
        seen = set()
        for ft in (species.get("flavor_text_entries") or []):
            lang    = (ft.get("language") or {}).get("name")
            version = (ft.get("version") or {}).get("name")
            text    = ft.get("flavor_text", "").replace("\n", " ").replace("\f", " ").strip()
            key     = (lang, version, text)
            if lang and version and text and key not in seen:
                seen.add(key)
                cur.execute("""
                    INSERT INTO pokemon_flavor_texts
                        (pokemon_id, language, version, flavor_text)
                    VALUES (%s, %s, %s, %s)
                """, (pokemon_id, lang, version, text))

        # ── 7. Moves (optional) ───────────────────────────────────────────────
        if include_moves:
            cur.execute("DELETE FROM pokemon_moves WHERE pokemon_id = %s", (pokemon_id,))
            for mv in poke.get("moves", []):
                move_name = mv["move"]["name"]
                for vg in mv.get("version_group_details", []):
                    cur.execute("""
                        INSERT INTO pokemon_moves
                            (pokemon_id, move_name, learn_method, level_learned, version_group)
                        VALUES (%s, %s, %s, %s, %s)
                    """, (
                        pokemon_id,
                        move_name,
                        vg["move_learn_method"]["name"],
                        vg.get("level_learned_at"),
                        vg["version_group"]["name"],
                    ))

        # ── 8. Forms ──────────────────────────────────────────────────────────
        cur.execute("DELETE FROM pokemon_forms WHERE pokemon_id = %s", (pokemon_id,))
        for form_ref in poke.get("forms", []):
            form_url  = form_ref.get("url", "")
            form_data = fetch(form_url)
            time.sleep(DELAY)
            f_sprites = form_data.get("sprites", {})
            cur.execute("""
                INSERT INTO pokemon_forms
                    (pokemon_id, form_name, form_order, is_default,
                     is_battle_only, is_mega, sprite_front, sprite_front_shiny)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                pokemon_id,
                form_data.get("form_name") or form_data.get("name") or "default",
                form_data.get("form_order"),
                form_data.get("is_default", False),
                form_data.get("is_battle_only", False),
                form_data.get("is_mega", False),
                f_sprites.get("front_default"),
                f_sprites.get("front_shiny"),
            ))


# ── Load evolution chains ─────────────────────────────────────────────────────

def load_evolution_chain(chain_id: int):
    """Load one evolution chain and all its evolution steps."""
    data = fetch(f"{POKEAPI}/evolution-chain/{chain_id}")
    time.sleep(DELAY)

    with db_cursor() as cur:
        # Upsert the chain record
        baby_item = (data.get("baby_trigger_item") or {}).get("name")
        cur.execute("""
            INSERT INTO pokemon_evolution_chains (id, baby_item)
            VALUES (%s, %s)
            ON CONFLICT (id) DO UPDATE SET baby_item = EXCLUDED.baby_item
        """, (chain_id, baby_item))

        # Walk the chain tree recursively
        def walk(node, from_id=None):
            if not node:
                return
            to_slug = (node.get("species") or {}).get("name")
            to_url  = (node.get("species") or {}).get("url", "")
            to_id   = extract_id_from_url(to_url)

            if from_id and to_id:
                for detail in (node.get("evolution_details") or [{}]):
                    cur.execute("""
                        INSERT INTO pokemon_evolutions (
                            chain_id, from_pokemon_id, to_pokemon_id,
                            trigger, min_level, item, held_item,
                            time_of_day, known_move, min_happiness,
                            min_beauty, min_affection,
                            needs_overworld_rain, turn_upside_down, gender
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s
                        )
                    """, (
                        chain_id, from_id, to_id,
                        (detail.get("trigger") or {}).get("name"),
                        detail.get("min_level"),
                        (detail.get("item") or {}).get("name"),
                        (detail.get("held_item") or {}).get("name"),
                        detail.get("time_of_day") or None,
                        (detail.get("known_move") or {}).get("name"),
                        detail.get("min_happiness"),
                        detail.get("min_beauty"),
                        detail.get("min_affection"),
                        detail.get("needs_overworld_rain", False),
                        detail.get("turn_upside_down", False),
                        detail.get("gender"),
                    ))

            for evolved in (node.get("evolves_to") or []):
                walk(evolved, to_id)

        walk(data.get("chain"))


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Load Pokémon data from PokeAPI")
    parser.add_argument("--moves",  action="store_true", help="Include moves (slow, large)")
    parser.add_argument("--limit",  type=int, default=0, help="Only load first N Pokémon (0=all)")
    parser.add_argument("--start",  type=int, default=1, help="Start from this Pokédex ID")
    parser.add_argument("--no-evolutions", action="store_true", help="Skip evolution chain loading")
    args = parser.parse_args()

    # PokeAPI's count includes forms/variants at non-sequential IDs.
    # Use 1025 (gen 9 total) unless --limit is specified.
    # Update this when a new generation releases.
    POKEDEX_MAX = 1025
    end = min(args.start + args.limit - 1, POKEDEX_MAX) if args.limit else POKEDEX_MAX
    if not args.limit:
        end = POKEDEX_MAX

    print(f"🎮 Loading Pokémon #{args.start}–{end} from PokeAPI")
    print(f"   Moves: {'yes (slow!)' if args.moves else 'no'}")
    print(f"   Evolutions: {'no' if args.no_evolutions else 'yes'}")
    print()

    # Track evolution chain IDs to load (avoid duplicates)
    evo_chains_to_load = set()
    loaded = 0
    errors = 0

    for pokemon_id in range(args.start, end + 1):
        try:
            # Quick pre-check: does this ID exist?
            r = requests.head(f"{POKEAPI}/pokemon/{pokemon_id}", timeout=10)
            if r.status_code == 404:
                print(f"  ⏭  #{pokemon_id} — not found (gap in Pokédex), skipping")
                continue

            print(f"  📥 #{pokemon_id:4d} / {end} ", end="", flush=True)
            load_pokemon(pokemon_id, include_moves=args.moves)
            loaded += 1
            print(f"✓")

            # Collect evolution chain IDs to load after
            if not args.no_evolutions:
                from db.connection import db_cursor as dbc
                with dbc() as cur:
                    cur.execute("SELECT evolution_chain_id FROM pokemon WHERE id = %s", (pokemon_id,))
                    row = cur.fetchone()
                    if row and row["evolution_chain_id"]:
                        evo_chains_to_load.add(row["evolution_chain_id"])

        except Exception as e:
            print(f"✗ ERROR: {e}")
            errors += 1
            if errors > 10:
                print("Too many errors — aborting. Fix connectivity and retry with --start.")
                sys.exit(1)
            continue

    # Load evolution chains
    if not args.no_evolutions and evo_chains_to_load:
        print(f"\n🔗 Loading {len(evo_chains_to_load)} evolution chains...")
        for chain_id in sorted(evo_chains_to_load):
            try:
                print(f"  Chain #{chain_id} ", end="", flush=True)
                load_evolution_chain(chain_id)
                print("✓")
            except Exception as e:
                print(f"✗ {e}")

    print(f"\n✅ Done! Loaded {loaded} Pokémon, {errors} errors.")
    if errors:
        print(f"   Re-run with --start to resume from where it failed.")


if __name__ == "__main__":
    main()
