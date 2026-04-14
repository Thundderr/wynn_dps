"""Reproduce the full Divzer build and feed our summed item stats to wb_compute.js.
If WB returns wynnbuilder's actual displayed numbers, our DPS formula is right
and the bug is purely in stat aggregation. Otherwise something's still off in calc."""
from __future__ import annotations
import json, subprocess, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wynn_dps.api import fetch_items
from wynn_dps.atree import apply_atree
from wynn_dps.craft import build_crafted_item
from wynn_dps.dps import Build, _build_ids
from wynn_dps.models import parse_all, parse_ingredient, parse_recipe
from wynn_dps.spells import load_spells

# Same crafted spec + atree node list as repro_divzer.py
from repro_divzer import CRAFTED, SP_ASSIGNED, ATREE_NODES, _wb_to_v3


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
    spells = load_spells()['archer']
    atree = apply_atree('archer', ATREE_NODES, spells)

    build = Build(weapon=weapon, armor=armor, accessories=accessories,
                  powders=[], skillpoints=SP_ASSIGNED,
                  atree_bonuses=atree.stat_bonuses,
                  atree_short_bonuses=atree.raw_short_bonuses,
                  atree_spells=atree.spells,
                  atree_spell_cost_delta=atree.spell_cost_delta,
                  atree_damage_mults=atree.damage_mults)
    eff = _build_ids(build)

    # Build the WB stat dict using SHORT names (wynnbuilder convention).
    v3_to_short = {
        'rawMainAttackDamage': 'mdRaw', 'mainAttackDamage': 'mdPct',
        'rawSpellDamage': 'sdRaw', 'spellDamage': 'sdPct',
        'rawStrength': 'str', 'rawDexterity': 'dex', 'rawIntelligence': 'int',
        'rawDefence': 'def', 'rawAgility': 'agi',
        'rawAttackSpeed': 'atkTier', 'criticalDamageBonus': 'critDamPct',
        'manaRegen': 'mr', 'manaSteal': 'ms', 'poison': 'poison',
        'earthDamage': 'eDamPct', 'thunderDamage': 'tDamPct',
        'waterDamage': 'wDamPct', 'fireDamage': 'fDamPct', 'airDamage': 'aDamPct',
        'earthMainAttackDamage': 'eMdPct', 'thunderMainAttackDamage': 'tMdPct',
        'waterMainAttackDamage': 'wMdPct', 'fireMainAttackDamage': 'fMdPct',
        'airMainAttackDamage': 'aMdPct',
        'earthSpellDamage': 'eSdPct', 'thunderSpellDamage': 'tSdPct',
        'waterSpellDamage': 'wSdPct', 'fireSpellDamage': 'fSdPct',
        'airSpellDamage': 'aSdPct',
    }
    wb_stats = {}
    for k, v in eff.items():
        short = v3_to_short.get(k)
        if short is not None:
            wb_stats[short] = wb_stats.get(short, 0) + v
    # Add atree-only short bonuses (e.g. tDamAddMin/Max)
    for k, v in build.atree_short_bonuses.items():
        wb_stats[k] = wb_stats.get(k, 0) + v
    # eff_int comes from str alongside dex (already mapped via 'int' from 'rawIntelligence')
    # Add assigned skillpoints to the effective totals
    wb_stats['str'] = SP_ASSIGNED['strength'] + wb_stats.get('str', 0)
    wb_stats['dex'] = SP_ASSIGNED['dexterity'] + wb_stats.get('dex', 0)
    wb_stats['int'] = SP_ASSIGNED['intelligence'] + wb_stats.get('int', 0)
    wb_stats['def'] = SP_ASSIGNED['defence'] + wb_stats.get('def', 0)
    wb_stats['agi'] = SP_ASSIGNED['agility'] + wb_stats.get('agi', 0)

    print("Effective skill points: str=%d dex=%d int=%d def=%d agi=%d" % (
        wb_stats['str'], wb_stats['dex'], wb_stats['int'],
        wb_stats['def'], wb_stats['agi']))
    print(f"mdRaw={wb_stats.get('mdRaw',0)}  mdPct={wb_stats.get('mdPct',0)}  "
          f"tDamPct={wb_stats.get('tDamPct',0)}  ")
    print(f"tDamAddMin={wb_stats.get('tDamAddMin',0)}  tDamAddMax={wb_stats.get('tDamAddMax',0)}")

    # Build WB spec
    spec = {
        "weapon": {"atkSpd": "SUPER_FAST", "tier": "Legendary",
                   "dam": {"n": [23,24], "e": [0,0], "t": [199,199],
                           "w": [0,0], "f": [0,0], "a": [0,0]}},
        "stats": wb_stats,
        "conversions": [105, 0, 0, 0, 0, 0],   # Bow Shot with Bow Proficiency
        "use_spell_damage": False,
        "ignore_speed": True,
        "part_filter": None,
        "ignore_str": False,
        "ignored_mults": [],
    }
    p = subprocess.run(["node", str(Path(__file__).parent / "wb_compute.js")],
        input=json.dumps(spec), capture_output=True, text=True, timeout=10)
    print("\n--- WB intermediates ---")
    print(p.stderr)
    out = json.loads(p.stdout)
    non_crit = (out[0][0] + out[0][1]) / 2
    crit = (out[1][0] + out[1][1]) / 2
    pc = 0.808
    avg = (1-pc)*non_crit + pc*crit
    print(f"\nWB non-crit avg = {non_crit:.2f}")
    print(f"WB crit avg     = {crit:.2f}")
    print(f"WB total avg    = {avg:.2f}")
    print(f"\nWynnbuilder displayed: 2860.81 per-attack")
    print(f"Difference: {avg - 2860.81:+.2f}")

if __name__ == "__main__":
    main()
