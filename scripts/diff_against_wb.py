"""Run wynnbuilder's exact JS calc + our Python calc on the same input and diff.

Usage: .venv/bin/python scripts/diff_against_wb.py
"""
from __future__ import annotations
import json, subprocess, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from wynn_dps.dps import (
    Build, _compute_part_damage, evaluate_spell, _build_ids,
)
from wynn_dps.spells import Spell, SpellPart
from wynn_dps.models import Item


def run_wb(spec):
    p = subprocess.run(
        ["node", str(Path(__file__).parent / "wb_compute.js")],
        input=json.dumps(spec), capture_output=True, text=True, timeout=10,
    )
    print("--- WB intermediates (stderr) ---")
    print(p.stderr)
    print("--- WB result ---")
    print(p.stdout)
    return json.loads(p.stdout)


# ---- Test case 1: Divzer alone, dex=152, no other items, no atree -----------

DIVZER_DAM = {"n": [23, 24], "e": [0, 0], "t": [199, 199],
              "w": [0, 0], "f": [0, 0], "a": [0, 0]}

# WB stats are short-named. For a Divzer-only build:
#   dex = 152 (152 from items if dex assigned 102 + raw from items 50; but here
#   we'll just give dex=152 as the effective stat)
WB_SPEC = {
    "weapon": {"atkSpd": "SUPER_FAST", "tier": "Legendary", "dam": DIVZER_DAM},
    "stats": {
        "str": 0, "dex": 152, "int": 0, "def": 0, "agi": 0,
        "mdRaw": 697, "mdPct": 0,
        "sdRaw": 0, "sdPct": 0,
        "damPct": 0, "damRaw": 0,
        "rDamPct": 0, "rDamRaw": 0, "rMdPct": 0, "rMdRaw": 0,
        "tDamPct": 0, "eDamPct": 0, "wDamPct": 0, "fDamPct": 0, "aDamPct": 0,
        "tMdPct": 0, "eMdPct": 0, "wMdPct": 0, "fMdPct": 0, "aMdPct": 0,
        "tDamRaw": 0, "eDamRaw": 0, "wDamRaw": 0, "fDamRaw": 0, "aDamRaw": 0,
        "tMdRaw": 0, "eMdRaw": 0, "wMdRaw": 0, "fMdRaw": 0, "aMdRaw": 0,
        "nDamAddMin": 0, "nDamAddMax": 0,
        "eDamAddMin": 0, "eDamAddMax": 0,
        "tDamAddMin": 0, "tDamAddMax": 0,
        "wDamAddMin": 0, "wDamAddMax": 0,
        "fDamAddMin": 0, "fDamAddMax": 0,
        "aDamAddMin": 0, "aDamAddMax": 0,
        "critDamPct": 0,
    },
    "conversions": [100, 0, 0, 0, 0, 0],   # plain melee 100% neutral
    "use_spell_damage": False,
    "ignore_speed": True,                  # per-attack number
    "part_filter": None,
    "ignore_str": False,
    "ignored_mults": [],
}


def main():
    print("\n========== WB GROUND TRUTH ==========")
    wb_out = run_wb(WB_SPEC)
    total_norm = wb_out[0]
    print(f"\nWB total non-crit:  min={total_norm[0]:.4f}  max={total_norm[1]:.4f}  mid={(total_norm[0]+total_norm[1])/2:.4f}")

    # ---- Reproduce in Python ------------------------------------------------
    print("\n========== OUR PYTHON ==========")
    weapon = Item(
        name="Divzer", type="weapon", sub_type="bow", tier="mythic",
        level=97, class_req="archer",
        skill_reqs={}, base_damage={"neutral": (23, 24), "thunder": (199, 199)},
        attack_speed="super_fast", powder_slots=3,
        ids={"rawMainAttackDamage": 697, "rawDexterity": 152},
    )
    build = Build(weapon=weapon, armor=[], accessories=[], powders=[],
                  skillpoints={"dexterity": 0})  # rawDex=152 already on weapon
    eff = _build_ids(build)
    print(f"Effective IDs: rawMain={eff.get('rawMainAttackDamage')}  rawDex={eff.get('rawDexterity')}")

    # Simulate Bow Shot: a single multiplier-100 part, melee scaling, no aps
    out = _compute_part_damage(build, [100,0,0,0,0,0],
                               use_spell_damage=False, use_atkspd=False,
                               use_str=True)
    # Compute WB's per-attack avg the same way it would (display.js:1525-1527).
    from wynn_dps.constants import skill_points_to_pct
    wb_non_crit = (wb_out[0][0] + wb_out[0][1]) / 2
    wb_crit    = (wb_out[1][0] + wb_out[1][1]) / 2
    crit_chance = skill_points_to_pct(152)
    wb_avg = (1 - crit_chance) * wb_non_crit + crit_chance * wb_crit
    print(f"\nOUR per-attack avg: {out!r}")
    print(f"WB  per-attack avg: {wb_avg!r}")
    print(f"Diff: {out - wb_avg!r}")
    if abs(out - wb_avg) < 1e-9:
        print("✓ EXACT match (within float precision)")
    elif abs(out - wb_avg) / max(wb_avg, 1) < 1e-6:
        print("✓ Match within float precision")
    else:
        print("✗ Numbers differ")

if __name__ == "__main__":
    main()
