"""Reproduce the wynnbuilder Divzer Boltslinger build and compare DPS.

Reference URL: https://wynnbuilder.github.io/builder/#CN00QkSudTvHmXXH2HIE2HYboOY4SvHmXXH2HIE2HYb2MY4SvH0J2J2R2R2RYW2SY441C9C9i9i9i92Am9I051f-6TQkuwdlsOXL-UhF8lezp0
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wynn_dps.api import fetch_items
from wynn_dps.atree import apply_atree
from wynn_dps.craft import build_crafted_item
from wynn_dps.cycle import melee_hits_per_second
from wynn_dps.dps import (
    Build, _build_ids, compute_melee_dps, compute_spell_cost, evaluate_spell,
)
from wynn_dps.models import (
    parse_all, parse_ingredient, parse_recipe,
)
from wynn_dps.spells import load_spells

REF = {
    'bow_per_attack': 2860.81,
    'bow_avg_dps': 1459.01,
    'arrow_storm_total': 213199.29,
    'arrow_storm_cost': 17.60,
    'arrow_bomb_total': 76080.05,
    'arrow_bomb_cost': 17.60,
    'escape_cost': 8.30,
    'guardian_angels_dps': 45196.23,
    'guardian_angels_cost': 7.83,
    'crit_chance': 0.808,
}

CRAFTED = {
    'leggings':  {'recipe_name': 'Leggings-100-103',
                   'ingredient_names': ["Bob's Tear", 'Decaying Heart', 'Cursed Wings',
                                        'Borange Fluff', 'Cursed Wings', 'Ritual Catalyst'],
                   'mat_tiers': (3, 3)},
    'boots':     {'recipe_name': 'Boots-100-103',
                   'ingredient_names': ["Bob's Tear", 'Decaying Heart', 'Cursed Wings',
                                        'Borange Fluff', 'Cursed Wings', 'Ritual Catalyst'],
                   'mat_tiers': (3, 3)},
    'ring1':     {'recipe_name': 'Ring-103-105',
                   'ingredient_names': ['Doom Stone', 'Doom Stone', 'Naval Stone',
                                        'Naval Stone', 'Naval Stone', 'Glowing Tree Sap'],
                   'mat_tiers': (3, 3)},
    'ring2':     {'recipe_name': 'Ring-103-105',
                   'ingredient_names': ['Doom Stone', 'Doom Stone', 'Naval Stone',
                                        'Naval Stone', 'Naval Stone', 'Glowing Tree Sap'],
                   'mat_tiers': (3, 3)},
}

SP_ASSIGNED = {'strength': 0, 'dexterity': 102, 'intelligence': 102,
               'defence': 0, 'agility': 0}

# Ability-tree node list inferred from the build description.
# Boltslinger archetype, with crossover into Sharpshooter masteries.
ATREE_NODES = [
    # Base spells (defines them in the engine).
    'Arrow Bomb', 'Escape', 'Arrow Storm', 'Arrow Shield',
    # Passives & masteries shown in the build text.
    'Bow Proficiency',
    'Air Mastery', 'Fire Mastery', 'Thunder Mastery',
    # Arrow Bomb upgrade chain.
    'Cheaper Arrow Bomb I', 'Heart Shatter', 'Grape Bomb', 'Shrapnel Bomb',
    'Bouncing Bomb', 'Cheaper Arrow Bomb II', 'Pyrotechnics',
    # Arrow Storm upgrades (Triple Shots gives extra streams; "Quintuple Arrows"
    # / "Arrow Hurricane" are the +arrows / +streams nodes — note Triple Shots
    # provides "+1 stream + 2 extra arrows per stream" properties).
    'Cheaper Arrow Storm I', 'Triple Shots', 'Cheaper Arrow Storm II', 'Arrow Hurricane',
    # Escape & Shield reductions and Guardian Angels upgrade chain.
    'Cheaper Escape I', 'Cheaper Escape II',
    'Cheaper Arrow Shield I', 'Cheaper Arrow Shield II',
    'Buckshot', 'Guardian Angels',
    'Vigilant Sentinels', 'Initiator',
    # Other passives mentioned.
    'Frenzy', 'Leap', 'Recycling', 'Elusive', 'Snow Storm',
]


def main():
    items_raw = fetch_items()
    items_by_name = {it.name: it for it in parse_all(items_raw)}

    helmet = items_by_name['Logistics']
    chest = items_by_name['Time Rift']
    bracelet = items_by_name['Supercharge']
    necklace = items_by_name['Amanuensis']
    weapon = items_by_name['Divzer']

    ings_raw = json.load(open('/Users/aidensmith/test-projects/wynnbuilder_ref/ingreds_clean.json'))
    ing_by_name = {i['displayName']: _wb_to_v3(i) for i in ings_raw}
    recipes_raw = json.load(open('/Users/aidensmith/test-projects/wynnbuilder_ref/recipes.json'))['recipes']
    rec_by_name = {r['name']: parse_recipe(r) for r in recipes_raw}

    crafted = {}
    for slot, spec in CRAFTED.items():
        rec = rec_by_name[spec['recipe_name']]
        ings = [parse_ingredient(nm, ing_by_name[nm]) for nm in spec['ingredient_names']]
        crafted[slot] = build_crafted_item(rec, ings, spec['mat_tiers'], 'normal')

    armor = [helmet, chest, crafted['leggings'], crafted['boots']]
    accessories = [crafted['ring1'], crafted['ring2'], bracelet, necklace]

    base_spells = load_spells()['archer']
    atree = apply_atree('archer', ATREE_NODES, base_spells)
    print(f"Atree applied: +{len(atree.stat_bonuses)} v3-named bonuses, "
          f"+{len(atree.raw_short_bonuses)} short-named bonuses, "
          f"{len(atree.spells)} spell defs, "
          f"{len(atree.spell_cost_delta)} cost deltas, "
          f"{len(atree.damage_mults)} damage mults.")

    build = Build(
        weapon=weapon, armor=armor, accessories=accessories,
        powders=[],  # no powders specified by the build
        skillpoints=SP_ASSIGNED,
        atree_bonuses=atree.stat_bonuses,
        atree_short_bonuses=atree.raw_short_bonuses,
        atree_spells=atree.spells,
        atree_spell_cost_delta=atree.spell_cost_delta,
        atree_damage_mults=atree.damage_mults,
    )

    eff = _build_ids(build)
    print("\nEffective IDs (items + atree):")
    for k in sorted(eff):
        if abs(eff[k]) > 0:
            print(f"  {k:30s} {eff[k]:>8.1f}")

    aps = melee_hits_per_second(build)
    dps = compute_melee_dps(build)
    per_hit = dps / aps if aps else 0
    print(f"\n--- Melee (Bow Shot) ---")
    print(f"  aps:      {aps:.3f} hits/s    (wynn says super_slow = 0.51)")
    print(f"  Per Atk:  ours={per_hit:>10.2f}  wynn={REF['bow_per_attack']:>10.2f}  Δ={per_hit - REF['bow_per_attack']:+10.2f}")
    print(f"  Avg DPS:  ours={dps:>10.2f}  wynn={REF['bow_avg_dps']:>10.2f}  Δ={dps - REF['bow_avg_dps']:+10.2f}")

    print(f"\n--- Spell costs ---")
    for slot, ref in [(1, 17.60), (2, 8.30), (3, 17.60), (4, 7.83)]:
        s = atree.spells.get(slot) or base_spells.get(slot)
        c = compute_spell_cost(build, slot, s.base_cost)
        print(f"  [{slot}] {s.name:<18}: ours={c:6.2f}  wynn={ref:6.2f}  Δ={c-ref:+6.2f}")

    print(f"\n--- Spell damage ---")
    for slot in (1, 3, 4):
        s = atree.spells.get(slot) or base_spells.get(slot)
        parts = evaluate_spell(build, s)
        if not s.parts:
            continue
        # Show all parts to debug
        print(f"  [{slot}] {s.name}: parts =")
        for pname, val in parts.items():
            print(f"      {pname:<30}: {val:>14,.2f}")


def _wb_to_v3(raw_ing):
    ids_legacy = raw_ing.get('ids', {})
    ids_translated = {}
    short_to_v3 = {
        'mdRaw': 'rawMainAttackDamage', 'mdPct': 'mainAttackDamage',
        'sdRaw': 'rawSpellDamage', 'sdPct': 'spellDamage',
        'eDamPct': 'earthDamage', 'tDamPct': 'thunderDamage',
        'wDamPct': 'waterDamage', 'fDamPct': 'fireDamage', 'aDamPct': 'airDamage',
        'str': 'rawStrength', 'dex': 'rawDexterity', 'int': 'rawIntelligence',
        'def': 'rawDefence', 'agi': 'rawAgility',
        'atkTier': 'rawAttackSpeed',
        'critDamPct': 'criticalDamageBonus',
        'mr': 'manaRegen', 'ms': 'manaSteal', 'poison': 'poison',
        'spRaw1': '1stSpellCost', 'spRaw2': '2ndSpellCost',
        'spRaw3': '3rdSpellCost', 'spRaw4': '4thSpellCost',
    }
    for short, v in ids_legacy.items():
        v3 = short_to_v3.get(short, short)
        if isinstance(v, dict):
            ids_translated[v3] = {'min': v.get('minimum', 0), 'raw': v.get('maximum', 0), 'max': v.get('maximum', 0)}
        else:
            ids_translated[v3] = v
    return {
        'type': 'ingredient',
        'requirements': {'level': raw_ing.get('lvl', 1),
                         'skills': [s.lower() for s in raw_ing.get('skills', [])]},
        'tier': f"TIER_{raw_ing.get('tier', 0)}",
        'identifications': ids_translated,
        'ingredientPositionModifiers': raw_ing.get('posMods', {}),
        'itemOnlyIDs': {
            'durabilityModifier': raw_ing.get('itemIDs', {}).get('dura', 0),
            'strengthRequirement': raw_ing.get('itemIDs', {}).get('strReq', 0),
            'dexterityRequirement': raw_ing.get('itemIDs', {}).get('dexReq', 0),
            'intelligenceRequirement': raw_ing.get('itemIDs', {}).get('intReq', 0),
            'defenceRequirement': raw_ing.get('itemIDs', {}).get('defReq', 0),
            'agilityRequirement': raw_ing.get('itemIDs', {}).get('agiReq', 0),
        },
    }


if __name__ == "__main__":
    main()
