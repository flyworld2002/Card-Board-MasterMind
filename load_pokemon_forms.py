#!/usr/bin/env python3
"""
load_pokemon_forms.py — Load alternate form Pokemon into the pokemon table.

Loads regional variants (Alolan, Galarian, Hisuian, Paldean),
Mega evolutions, Gigantamax, and other meaningful forms.
Sets base_pokemon_id to link each form back to its base Pokemon.

Skips cosmetic-only forms (Unown letters, Vivillon patterns,
Pikachu cap variants, Spinda, etc.) — these have no distinct
card-relevant stats or typing differences.

Usage:
    python3 load_pokemon_forms.py           # load all meaningful forms
    python3 load_pokemon_forms.py --dry-run # show what would be loaded
    python3 load_pokemon_forms.py --limit 20

No API key required. Rate-limits itself politely.
"""

import argparse
import time
import re
import sys
import requests
from db.connection import db_cursor

POKEAPI = "https://pokeapi.co/api/v2"
DELAY   = 0.3

# ── Forms to skip (cosmetic only, no meaningful stat/type differences) ────────
SKIP_SLUGS = {
    # Unown letters (28 forms, all identical stats)
    *[f"unown-{c}" for c in "abcdefghijklmnopqrstuvwxyz!?"],
    # Vivillon wing patterns (20 forms, identical stats)
    *[f"vivillon-{p}" for p in [
        "archipelago","continental","elegant","fancy","garden","high-plains",
        "icy-snow","jungle","marine","meadow","modern","monsoon","ocean",
        "polar","river","sandstorm","savanna","sun","tundra","poke-ball"
    ]],
    # Pikachu cap variants (7 forms, identical stats)
    *[f"pikachu-{c}" for c in [
        "original-cap","hoenn-cap","sinnoh-cap","unova-cap",
        "kalos-cap","alola-cap","world-cap","partner-cap","starter"
    ]],
    # Spinda (identical stats, just dot patterns)
    *[f"spinda-{n:02d}" for n in range(1, 9)],
    # Minior cores (same stats as shield form, just colors)
    *[f"minior-{c}-core" for c in [
        "red","orange","yellow","green","blue","indigo","violet"
    ]],
    # Alcremie decorations
    *[f"alcremie-{f}" for f in [
        "ruby-cream","matcha-cream","mint-cream","lemon-cream",
        "salted-cream","ruby-swirl","caramel-swirl","rainbow-swirl"
    ]],
    # Furfrou trims (cosmetic only)
    *[f"furfrou-{t}" for t in [
        "heart","star","diamond","debutante","matron","dandy",
        "la-reine","kabuki","pharaoh"
    ]],
    # Floette eternal (unobtainable)
    "floette-eternal",
    # Eternal Flower Floette
    "floette-eternal-flower",
}

# ── Form categories we DO want ────────────────────────────────────────────────
# These have meaningfully different typings or stats worth storing
MEANINGFUL_FORM_KEYWORDS = [
    "alolan", "alola",
    "galarian", "galar",
    "hisuian", "hisui",
    "paldean", "paldea",
    "mega",
    "gigantamax",
    "gmax",
    "origin",       # Giratina-Origin, Dialga-Origin, Palkia-Origin
    "therian",      # Tornadus/Thundurus/Landorus/Enamorus
    "black",        # Kyurem-Black, Zekrom
    "white",        # Kyurem-White
    "sky",          # Shaymin-Sky
    "pirouette",    # Meloetta-Pirouette
    "complete",     # Zygarde-Complete
    "ultra",        # Necrozma-Ultra
    "dusk-mane",    # Necrozma-Dusk-Mane
    "dawn-wings",   # Necrozma-Dawn-Wings
    "primal",       # Groudon-Primal, Kyogre-Primal
    "hero",         # Palafin-Hero
    "bloodmoon",    # Ursaluna-Bloodmoon
    "combat",       # Ogerpon-Wellspring etc
    "wellspring",
    "hearthflame",
    "cornerstone",
    "teal",         # Ogerpon-Teal
    "crowned",      # Zacian-Crowned, Zamazenta-Crowned
    "eternamax",    # Eternatus-Eternamax
    "ash",          # Greninja-Ash
    "battle-bond",
    "power-construct", # Zygarde
    "school",       # Wishiwashi-School
    "disguised",    # Mimikyu
    "busted",       # Mimikyu-Busted
    "totem",        # Totem forms have different stats
    "starter",
    "wormadam",     # Wormadam has different types per form
    "sandy",
    "trash",
    "rotom",        # Rotom appliances have different types
    "heat", "wash", "frost", "fan", "mow",
    "attack",       # Deoxys
    "defense",
    "speed",
    "blade",        # Aegislash-Blade
    "zen",          # Darmanitan-Zen
    "galar-zen",
    "family-of-three", # Maushold
    "four",
    "two-segment",  # Dudunsparce
    "three-segment",
    "hangry",       # Morpeko-Hangry
    "noice",        # Eiscue-Noice
    "gorging",      # Cramorant
    "gulping",
    "low-key",      # Toxtricity-Low-Key
    "amped",
    "rapid-strike", # Urshifu
    "single-strike",
    "ice",          # Darmanitan-Ice (Galarian Zen)
    "shadow-rider", # Calyrex
    "ice-rider",
    "black-kyurem",
    "white-kyurem",
    "resolute",     # Keldeo-Resolute
    "pirouette",    # Meloetta-Pirouette
    "pop-star",     # Mimikyu etc
]


def fetch(url: str, retries: int = 3) -> dict:
    for attempt in range(retries):
        try:
            r = requests.get(url, timeout=15)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            if attempt < retries - 1:
                print(f"    ⚠️  Retry {attempt+1}: {e}")
                time.sleep(2 ** attempt)
            else:
                raise
    return None


def extract_id_from_url(url: str) -> int | None:
    m = re.search(r'/(\d+)/?$', url)
    return int(m.group(1)) if m else None


def title_case(s: str) -> str:
    """Convert PokeAPI slug to proper display name."""

    EXPLICIT = {
        # Special base names
        "mr-mime": "Mr. Mime", "mr-rime": "Mr. Rime",
        "mime-jr": "Mime Jr.", "ho-oh": "Ho-Oh",
        "porygon-z": "Porygon-Z", "type-null": "Type: Null",
        "jangmo-o": "Jangmo-o", "hakamo-o": "Hakamo-o", "kommo-o": "Kommo-o",
        "wo-chien": "Wo-Chien", "chien-pao": "Chien-Pao",
        "ting-lu": "Ting-Lu", "chi-yu": "Chi-Yu",
        # Gender variants
        "meowstic-male": "Male Meowstic", "meowstic-female": "Female Meowstic",
        "indeedee-male": "Male Indeedee", "indeedee-female": "Female Indeedee",
        "basculegion-male": "Male Basculegion", "basculegion-female": "Female Basculegion",
        "oinkologne-male": "Male Oinkologne", "oinkologne-female": "Female Oinkologne",
        "meowstic-mega-male": "Male Mega Meowstic",
        "meowstic-mega-female": "Female Mega Meowstic",
        # Magearna
        "magearna-original": "Original Color Magearna",
        "magearna-mega-original": "Original Mega Magearna",
        # Minior meteor forms — color first, Minior last
        "minior-red-meteor":    "Red Meteor Minior",
        "minior-orange-meteor": "Orange Meteor Minior",
        "minior-yellow-meteor": "Yellow Meteor Minior",
        "minior-green-meteor":  "Green Meteor Minior",
        "minior-blue-meteor":   "Blue Meteor Minior",
        "minior-indigo-meteor": "Indigo Meteor Minior",
        "minior-violet-meteor": "Violet Meteor Minior",
        # Minior core forms — color first, Minior last
        "minior-red":    "Red Minior",
        "minior-orange": "Orange Minior",
        "minior-yellow": "Yellow Minior",
        "minior-green":  "Green Minior",
        "minior-blue":   "Blue Minior",
        "minior-indigo": "Indigo Minior",
        "minior-violet": "Violet Minior",
        # Floette
        "floette-eternal": "Eternal Floette",
        # Tatsugiri
        "tatsugiri-curly": "Curly Tatsugiri",
        "tatsugiri-droopy": "Droopy Tatsugiri",
        "tatsugiri-stretchy": "Stretchy Tatsugiri",
        # Tatsugiri mega — actual PokeAPI slug order (variant before mega)
        "tatsugiri-curly-mega": "Curly Mega Tatsugiri",
        "tatsugiri-droopy-mega": "Droopy Mega Tatsugiri",
        "tatsugiri-stretchy-mega": "Stretchy Mega Tatsugiri",
        # Magearna — actual PokeAPI slug order
        "magearna-original-mega": "Origin Mega Magearna",
        "tatsugiri-mega-curly": "Curly Mega Tatsugiri",
        "tatsugiri-mega-droopy": "Droopy Mega Tatsugiri",
        "tatsugiri-mega-stretchy": "Stretchy Mega Tatsugiri",
        # Ogerpon
        "ogerpon-teal-mask": "Teal Mask Ogerpon",
        "ogerpon-wellspring-mask": "Wellspring Mask Ogerpon",
        "ogerpon-hearthflame-mask": "Hearthflame Mask Ogerpon",
        "ogerpon-cornerstone-mask": "Cornerstone Mask Ogerpon",
        # Dudunsparce
        "dudunsparce-two-segment": "Two Segment Dudunsparce",
        "dudunsparce-three-segment": "Three Segment Dudunsparce",
        # Palafin / Maushold / Squawkabilly
        "palafin-hero": "Hero Palafin",
        "maushold-family-of-three": "Family of Three Maushold",
        "maushold-family-of-four": "Family of Four Maushold",
        "squawkabilly-green-plumage": "Green Plumage Squawkabilly",
        "squawkabilly-blue-plumage": "Blue Plumage Squawkabilly",
        "squawkabilly-yellow-plumage": "Yellow Plumage Squawkabilly",
        "squawkabilly-white-plumage": "White Plumage Squawkabilly",
        # Toxtricity
        "toxtricity-amped": "Amped Toxtricity",
        "toxtricity-low-key": "Low Key Toxtricity",
        "toxtricity-amped-gmax": "Gigantamax Amped Toxtricity",
        "toxtricity-low-key-gmax": "Gigantamax Low Key Toxtricity",
        # Urshifu
        "urshifu-single-strike": "Single Strike Urshifu",
        "urshifu-rapid-strike": "Rapid Strike Urshifu",
        "urshifu-single-strike-gmax": "Gigantamax Single Strike Urshifu",
        "urshifu-rapid-strike-gmax": "Gigantamax Rapid Strike Urshifu",
        # Calyrex — both slug variants
        "calyrex-ice-rider": "Ice Rider Calyrex",    # guess
        "calyrex-shadow-rider": "Shadow Rider Calyrex", # guess
        "calyrex-ice": "Ice Rider Calyrex",           # actual PokeAPI slug
        "calyrex-shadow": "Shadow Rider Calyrex",     # actual PokeAPI slug
        # Zacian / Zamazenta
        "zacian-crowned": "Crowned Sword Zacian",
        "zamazenta-crowned": "Crowned Shield Zamazenta",
        # Necrozma
        "necrozma-dusk-mane": "Dusk Mane Necrozma",
        "necrozma-dawn-wings": "Dawn Wings Necrozma",
        "necrozma-ultra": "Ultra Necrozma",
        # Kyurem
        "kyurem-black": "Black Kyurem",
        "kyurem-white": "White Kyurem",
        # Darmanitan
        "darmanitan-zen": "Zen Mode Darmanitan",
        "darmanitan-galar": "Galarian Darmanitan",
        "darmanitan-galar-standard": "Galarian Darmanitan",
        "darmanitan-galar-zen": "Galarian Zen Mode Darmanitan",
        # Lycanroc
        "lycanroc-midday": "Midday Lycanroc",
        "lycanroc-midnight": "Midnight Lycanroc",
        "lycanroc-dusk": "Dusk Lycanroc",
        # Wishiwashi / Mimikyu / Morpeko / Eiscue / Cramorant
        "wishiwashi-school": "School Form Wishiwashi",
        "mimikyu-disguised": "Mimikyu",
        "mimikyu-busted": "Busted Mimikyu",
        "morpeko-full-belly": "Morpeko",
        "morpeko-hangry": "Hangry Morpeko",
        "eiscue-ice": "Eiscue",
        "eiscue-noice": "Noice Face Eiscue",
        "cramorant-gulping": "Gulping Cramorant",
        "cramorant-gorging": "Gorging Cramorant",
        # Aegislash
        "aegislash-shield": "Aegislash",
        "aegislash-blade": "Blade Aegislash",
        # Zygarde
        "zygarde-10": "10% Zygarde",
        "zygarde-50": "Zygarde",
        "zygarde-complete": "Complete Zygarde",
        # Greninja
        "greninja-ash":         "Ash-Greninja",
        "greninja-battle-bond": "Battle Bond Greninja",
        # Shaymin / Giratina / Deoxys / Meloetta / Keldeo
        "shaymin-land": "Shaymin", "shaymin-sky": "Sky Shaymin",
        "giratina-altered": "Giratina", "giratina-origin": "Origin Giratina",
        "deoxys-normal": "Deoxys", "deoxys-attack": "Attack Deoxys",
        "deoxys-defense": "Defense Deoxys", "deoxys-speed": "Speed Deoxys",
        "meloetta-aria": "Meloetta", "meloetta-pirouette": "Pirouette Meloetta",
        "keldeo-ordinary": "Keldeo", "keldeo-resolute": "Resolute Keldeo",
        # Therian forms
        "tornadus-therian": "Therian Tornadus",
        "thundurus-therian": "Therian Thundurus",
        "landorus-therian": "Therian Landorus",
        "enamorus-therian": "Therian Enamorus",
        # Wormadam / Rotom
        "wormadam-plant": "Wormadam",
        "wormadam-sandy": "Sandy Cloak Wormadam",
        "wormadam-trash": "Trash Cloak Wormadam",
        "rotom-heat": "Heat Rotom", "rotom-wash": "Wash Rotom",
        "rotom-frost": "Frost Rotom", "rotom-fan": "Fan Rotom",
        "rotom-mow": "Mow Rotom",
        # Ursaluna / Eternatus
        "ursaluna-bloodmoon": "Bloodmoon Ursaluna",
        "eternatus-eternamax": "Eternamax Eternatus",
        # Castform weather forms (different typings per weather)
        "castform-sunny":  "Sunny Castform",
        "castform-rainy":  "Rainy Castform",
        "castform-snowy":  "Snowy Castform",
        # Hoopa
        "hoopa-unbound":   "Unbound Hoopa",
        # Oricorio forms (different typings per form)
        "oricorio-pom-pom":  "Pom-Pom Oricorio",
        "oricorio-pau":      "Pa'u Oricorio",
        "oricorio-sensu":    "Sensu Oricorio",
        # Rockruff
        "rockruff-own-tempo": "Own Tempo Rockruff",
        # Necrozma fusions — actual PokeAPI slugs (shorter than expected)
        "necrozma-dusk":     "Dusk Mane Necrozma",
        "necrozma-dawn":     "Dawn Wings Necrozma",
        # Zarude
        "zarude-dada":       "Dada Zarude",
        # Gimmighoul
        "gimmighoul-roaming": "Roaming Gimmighoul",
        # Koraidon battle builds
        "koraidon-limited-build":   "Limited Build Koraidon",
        "koraidon-sprinting-build": "Sprinting Build Koraidon",
        "koraidon-swimming-build":  "Swimming Build Koraidon",
        "koraidon-gliding-build":   "Gliding Build Koraidon",
        # Miraidon battle modes
        "miraidon-low-power-mode":  "Low Power Miraidon",
        "miraidon-drive-mode":      "Drive Mode Miraidon",
        "miraidon-aquatic-mode":    "Aquatic Mode Miraidon",
        "miraidon-glide-mode":      "Glide Mode Miraidon",
        # Terapagos
        "terapagos-terastal":  "Terastal Terapagos",
        "terapagos-stellar":   "Stellar Terapagos",
        # Pumpkaboo / Gourgeist sizes (meaningfully different stats)
        "pumpkaboo-small":   "Small Pumpkaboo",
        "pumpkaboo-large":   "Large Pumpkaboo",
        "pumpkaboo-super":   "Super Size Pumpkaboo",
        "gourgeist-small":   "Small Gourgeist",
        "gourgeist-large":   "Large Gourgeist",
        "gourgeist-super":   "Super Size Gourgeist",
        # Paldean forms
        "tauros-paldea-combat-breed": "Paldean Combat Tauros",
        "tauros-paldea-blaze-breed": "Paldean Blaze Tauros",
        "tauros-paldea-aqua-breed": "Paldean Aqua Tauros",
        "wooper-paldea": "Paldean Wooper",
        "slowpoke-galar": "Galarian Slowpoke",
        "slowbro-galar": "Galarian Slowbro",
        "slowking-galar": "Galarian Slowking",
        # Pikachu cap variants — descriptor first, Pikachu last
        "pikachu-original-cap":  "Original Cap Pikachu",
        "pikachu-hoenn-cap":     "Hoenn Cap Pikachu",
        "pikachu-sinnoh-cap":    "Sinnoh Cap Pikachu",
        "pikachu-unova-cap":     "Unova Cap Pikachu",
        "pikachu-kalos-cap":     "Kalos Cap Pikachu",
        "pikachu-alola-cap":     "Alolan Cap Pikachu",
        "pikachu-partner-cap":   "Partner Cap Pikachu",
        "pikachu-world-cap":     "World Cap Pikachu",
        "pikachu-starter":       "Starter Pikachu",
        # Pikachu special forms
        "pikachu-pop-star": "Pop Star Pikachu",
        "pikachu-rock-star": "Rock Star Pikachu",
        "pikachu-belle": "Belle Pikachu",
        "pikachu-phd": "PhD Pikachu",
        "pikachu-libre": "Libre Pikachu",
        "pikachu-cosplay": "Cosplay Pikachu",
        # Totem forms
        "raticate-totem-alola": "Alolan Totem Raticate",
        "mimikyu-totem-disguised": "Disguised Totem Mimikyu",
        "mimikyu-totem-busted": "Busted Totem Mimikyu",
        "marowak-totem": "Totem Marowak",
        "kommo-o-totem": "Totem Kommo-o",
        "ribombee-totem": "Totem Ribombee",
        "vikavolt-totem": "Totem Vikavolt",
        "lurantis-totem": "Totem Lurantis",
        "salazzle-totem": "Totem Salazzle",
        "wishiwashi-totem": "Totem Wishiwashi",
        "togedemaru-totem": "Totem Togedemaru",
        # Starter variants
        "eevee-starter": "Starter Eevee",
        "pikachu-starter": "Starter Pikachu",
        # Origin forms
        "dialga-origin": "Origin Dialga",
        "palkia-origin": "Origin Palkia",
        # Crabominable mega
        "crabominable-mega": "Mega Crabominable",
        # Tauros Paldean breeds
        "tauros-paldea-combat-breed": "Paldean Combat Breed Tauros",
        "tauros-paldea-blaze-breed": "Paldean Blaze Breed Tauros",
        "tauros-paldea-aqua-breed": "Paldean Aqua Breed Tauros",
        # Basculin
        "basculin-red-striped": "Red Striped Basculin",
        "basculin-blue-striped": "Blue Striped Basculin",
        "basculin-white-striped": "White Striped Basculin",
        # Zygarde power construct
        "zygarde-10-power-construct": "10% Power Construct Zygarde",
        "zygarde-50-power-construct": "50% Power Construct Zygarde",
        # Magearna
        "magearna-mega-original": "Origin Mega Magearna",
        # Meowstic mega — both slug orderings
        "meowstic-mega-male": "Male Mega Meowstic",    # guess
        "meowstic-mega-female": "Female Mega Meowstic", # guess
        "meowstic-male-mega": "Male Mega Meowstic",    # actual PokeAPI slug
        "meowstic-female-mega": "Female Mega Meowstic", # actual PokeAPI slug
        # Tatsugiri mega
        "tatsugiri-mega-curly": "Curly Mega Tatsugiri",
        "tatsugiri-mega-droopy": "Droopy Mega Tatsugiri",
        "tatsugiri-mega-stretchy": "Stretchy Mega Tatsugiri",
    }

    if s in EXPLICIT:
        return EXPLICIT[s]

    # Generic form prefix reordering for anything not in the explicit map
    FORM_PREFIXES = {
        "mega": "Mega", "alola": "Alolan", "galar": "Galarian",
        "hisui": "Hisuian", "paldea": "Paldean", "gmax": "Gigantamax",
        "primal": "Primal", "eternamax": "Eternamax", "totem": "Totem",
    }

    parts = s.split("-")
    if len(parts) >= 2:
        for i, part in enumerate(parts[1:], 1):
            if part in FORM_PREFIXES:
                base  = " ".join(w.capitalize() for w in parts[:i])
                form  = FORM_PREFIXES[part]
                extra = " ".join(
                    w.upper() if len(w) == 1 else w.capitalize()
                    for w in parts[i+1:]
                )
                return f"{form} {base}{' ' + extra if extra else ''}".strip()

    return " ".join(
        w.upper() if len(w) == 1 else w.capitalize()
        for w in parts
    )


def _default_title(slug):
    """What title_case would return with no explicit mapping."""
    return " ".join(
        w.upper() if len(w) == 1 else w.capitalize()
        for w in slug.split("-")
    )


def is_meaningful_form(slug: str) -> bool:
    """Return True if this form has card-relevant differences worth storing."""
    if slug in SKIP_SLUGS:
        return False
    # If title_case produces a different result than the plain default,
    # an explicit mapping exists — always treat as meaningful
    if title_case(slug) != _default_title(slug):
        return True
    slug_lower = slug.lower()
    return any(kw in slug_lower for kw in MEANINGFUL_FORM_KEYWORDS)


def get_base_pokemon_id(species_url: str) -> int | None:
    """Get the base Pokemon ID (Pokedex number) from the species URL."""
    species_id = extract_id_from_url(species_url)
    if species_id and species_id <= 1025:
        return species_id
    return None


def load_form(form_id: int, base_id: int, dry_run: bool = False) -> bool:
    """Load one alternate form Pokemon. Returns True if loaded."""
    data = fetch(f"{POKEAPI}/pokemon/{form_id}")
    if not data:
        return False

    slug = data.get("name", "")
    if not is_meaningful_form(slug):
        return False

    name = title_case(slug)

    if dry_run:
        print(f"    Would load: #{form_id} {name} (base: #{base_id})")
        return True

    # Get species data for generation, legendary status etc.
    species_url = (data.get("species") or {}).get("url", "")
    species = fetch(species_url) if species_url else {}
    if species:
        time.sleep(DELAY)

    sprites = data.get("sprites", {})
    other   = sprites.get("other", {})
    sprite_official = (other.get("official-artwork") or {}).get("front_default")

    stats = {s["stat"]["name"]: s["base_stat"] for s in data.get("stats", [])}

    gen_url = (species.get("generation") or {}).get("url", "") if species else ""
    gen_id  = extract_id_from_url(gen_url)

    with db_cursor() as cur:
        cur.execute("""
            INSERT INTO pokemon (
                id, name, slug, base_pokemon_id,
                generation, is_legendary, is_mythical, is_baby,
                height_dm, weight_hg, base_experience,
                base_hp, base_attack, base_defense,
                base_sp_atk, base_sp_def, base_speed,
                sprite_official, sprite_front, sprite_front_shiny
            ) VALUES (
                %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s, %s, %s, %s,
                %s, %s, %s
            )
            ON CONFLICT (id) DO UPDATE SET
                name             = EXCLUDED.name,
                base_pokemon_id  = EXCLUDED.base_pokemon_id,
                generation       = EXCLUDED.generation,
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
            form_id, name, slug, base_id,
            gen_id,
            species.get("is_legendary", False) if species else False,
            species.get("is_mythical", False) if species else False,
            species.get("is_baby", False) if species else False,
            data.get("height"), data.get("weight"),
            data.get("base_experience"),
            stats.get("hp"), stats.get("attack"), stats.get("defense"),
            stats.get("special-attack"), stats.get("special-defense"),
            stats.get("speed"),
            sprite_official,
            sprites.get("front_default"),
            sprites.get("front_shiny"),
        ))

        # Load types for this form (may differ from base — e.g. Alolan Raichu)
        cur.execute("DELETE FROM pokemon_types WHERE pokemon_id = %s", (form_id,))
        for t in data.get("types", []):
            cur.execute("""
                INSERT INTO pokemon_types (pokemon_id, slot, type_name)
                VALUES (%s, %s, %s) ON CONFLICT DO NOTHING
            """, (form_id, t["slot"], t["type"]["name"]))

    return True


def main():
    parser = argparse.ArgumentParser(
        description="Load alternate form Pokemon from PokeAPI"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be loaded without writing to DB")
    parser.add_argument("--limit", type=int, default=0,
                        help="Max forms to load (0=all)")
    args = parser.parse_args()

    print("🔄 Fetching full Pokemon list from PokeAPI...")
    # Get all pokemon entries including forms (up to 2000)
    data = fetch(f"{POKEAPI}/pokemon?limit=2000&offset=0")
    if not data:
        print("Failed to fetch Pokemon list")
        sys.exit(1)

    all_entries = data.get("results", [])

    # Filter to form IDs only (above 1025)
    form_entries = []
    for entry in all_entries:
        url = entry.get("url", "")
        pid = extract_id_from_url(url)
        if pid and pid > 1025:
            form_entries.append((pid, entry["name"]))

    form_entries.sort()
    print(f"Found {len(form_entries)} entries above ID 1025")

    # Load ALL forms — no filtering
    meaningful = form_entries
    print(f"Forms to load: {len(meaningful)}")

    if args.dry_run:
        print("\n--- DRY RUN ---")

    if args.limit:
        meaningful = meaningful[:args.limit]

    loaded  = 0
    skipped = 0
    errors  = 0

    for pid, slug in meaningful:
        # Get base pokemon ID from the species link
        # We need to fetch the pokemon to get its species URL
        try:
            poke_data = fetch(f"{POKEAPI}/pokemon/{pid}")
            if not poke_data:
                continue
            time.sleep(DELAY)

            species_url = (poke_data.get("species") or {}).get("url", "")
            base_id     = get_base_pokemon_id(species_url)

            if not base_id:
                print(f"  ⚠️  #{pid} {slug} — could not determine base ID, skipping")
                skipped += 1
                continue

            print(f"  📥 #{pid:6d} {title_case(slug):<35} base=#{base_id}", end="", flush=True)

            if args.dry_run:
                print(" [dry run]")
                loaded += 1
                continue

            # load_form will re-fetch, but we pass the already-fetched data
            # by calling it with the pre-fetched poke_data
            name = title_case(slug)
            stats = {s["stat"]["name"]: s["base_stat"]
                     for s in poke_data.get("stats", [])}
            sprites = poke_data.get("sprites", {})
            other   = sprites.get("other", {})
            sprite_official = (other.get("official-artwork") or {}).get("front_default")

            with db_cursor() as cur:
                cur.execute("""
                    INSERT INTO pokemon (
                        id, name, slug, base_pokemon_id,
                        height_dm, weight_hg, base_experience,
                        base_hp, base_attack, base_defense,
                        base_sp_atk, base_sp_def, base_speed,
                        sprite_official, sprite_front, sprite_front_shiny
                    ) VALUES (
                        %s, %s, %s, %s,
                        %s, %s, %s,
                        %s, %s, %s, %s, %s, %s,
                        %s, %s, %s
                    )
                    ON CONFLICT (id) DO UPDATE SET
                        name            = EXCLUDED.name,
                        base_pokemon_id = EXCLUDED.base_pokemon_id,
                        base_hp         = EXCLUDED.base_hp,
                        base_attack     = EXCLUDED.base_attack,
                        base_defense    = EXCLUDED.base_defense,
                        base_sp_atk     = EXCLUDED.base_sp_atk,
                        base_sp_def     = EXCLUDED.base_sp_def,
                        base_speed      = EXCLUDED.base_speed,
                        sprite_official = EXCLUDED.sprite_official,
                        sprite_front    = EXCLUDED.sprite_front,
                        sprite_front_shiny = EXCLUDED.sprite_front_shiny
                """, (
                    pid, name, slug, base_id,
                    poke_data.get("height"), poke_data.get("weight"),
                    poke_data.get("base_experience"),
                    stats.get("hp"), stats.get("attack"),
                    stats.get("defense"), stats.get("special-attack"),
                    stats.get("special-defense"), stats.get("speed"),
                    sprite_official,
                    sprites.get("front_default"),
                    sprites.get("front_shiny"),
                ))

                # Types (may differ from base form)
                cur.execute(
                    "DELETE FROM pokemon_types WHERE pokemon_id = %s", (pid,)
                )
                for t in poke_data.get("types", []):
                    cur.execute("""
                        INSERT INTO pokemon_types (pokemon_id, slot, type_name)
                        VALUES (%s, %s, %s) ON CONFLICT DO NOTHING
                    """, (pid, t["slot"], t["type"]["name"]))

            print(" ✓")
            loaded += 1

        except Exception as e:
            print(f" ✗ ERROR: {e}")
            errors += 1
            if errors > 10:
                print("Too many errors — aborting.")
                sys.exit(1)

    print(f"\n✅ Done! Loaded: {loaded} | Skipped: {skipped} | Errors: {errors}")
    print(f"\nNext step: run seed_characters.sql to populate the characters table")


if __name__ == "__main__":
    main()
