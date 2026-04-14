"""Crafting math, ported from wynnbuilder_ref/js/craft.js.

Given a Recipe, a list of 6 Ingredients (position-sensitive), material tiers
(1-3) for each of the 2 slots, and an attack speed (weapons only), build a
CraftedItem that flows through the same DPS pipeline as a regular Item.
"""
from __future__ import annotations

from typing import Sequence

from .constants import (
    ATKSPEED_CRAFT_RATIO, INGREDIENT_SLOT_GRID, MATERIAL_TIER_MULT,
    SKP_ELEMENTS, SKP_ORDER,
)
from .models import CraftedItem, INGREDIENT_ID_MAP, Ingredient, Recipe

# Mapping crafting item_type -> (Item.type, Item.sub_type)
_TYPE_MAP = {
    "HELMET": ("armour", "helmet"),
    "CHESTPLATE": ("armour", "chestplate"),
    "LEGGINGS": ("armour", "leggings"),
    "BOOTS": ("armour", "boots"),
    "RING": ("accessory", "ring"),
    "BRACELET": ("accessory", "bracelet"),
    "NECKLACE": ("accessory", "necklace"),
    "SPEAR": ("weapon", "spear"),
    "WAND": ("weapon", "wand"),
    "BOW": ("weapon", "bow"),
    "DAGGER": ("weapon", "dagger"),
    "RELIK": ("weapon", "relik"),
}


def _effectiveness_grid(ingreds: Sequence[Ingredient]) -> list[int]:
    """Return the flattened 3×2 effectiveness grid (indices 0..5)."""
    eff = [[100, 100], [100, 100], [100, 100]]
    for n, ing in enumerate(ingreds):
        i, j = INGREDIENT_SLOT_GRID[n]
        for key, val in ing.pos_mods.items():
            if val == 0:
                continue
            if key == "above":
                for k in range(i - 1, -1, -1):
                    eff[k][j] += val
            elif key == "under":
                for k in range(i + 1, 3):
                    eff[k][j] += val
            elif key == "left" and j == 1:
                eff[i][0] += val
            elif key == "right" and j == 0:
                eff[i][1] += val
            elif key == "touching":
                for k in range(3):
                    for l in range(2):
                        if (abs(k - i) == 1 and l == j) or (k == i and abs(l - j) == 1):
                            eff[k][l] += val
            elif key == "notTouching":
                for k in range(3):
                    for l in range(2):
                        if abs(k - i) > 1 or (abs(k - i) == 1 and abs(l - j) == 1):
                            eff[k][l] += val
    return [eff[i][j] for i, j in INGREDIENT_SLOT_GRID]


def _powder_slots_for_level(level: int) -> int:
    # craft.js:251-257
    if level < 30:
        return 1
    if level < 70:
        return 2
    return 3


def build_crafted_item(
    recipe: Recipe,
    ingredients: Sequence[Ingredient],
    material_tiers: tuple[int, int] = (3, 3),
    atk_speed: str = "normal",
) -> CraftedItem:
    if len(ingredients) != 6:
        raise ValueError("need exactly 6 ingredients (use 'No Ingredient' sentinels)")

    mapped = _TYPE_MAP.get(recipe.item_type)
    if mapped is None:
        raise ValueError(f"unsupported recipe type: {recipe.item_type}")
    item_type, sub_type = mapped
    is_weapon = item_type == "weapon"

    # 1. Material multiplier (craft.js:309-314).
    t0, t1 = material_tiers
    a0, a1 = recipe.material_amounts
    matmult = (
        MATERIAL_TIER_MULT[t0] * a0 + MATERIAL_TIER_MULT[t1] * a1
    ) / (a0 + a1)

    # 2. Base damage for weapons (craft.js:327-340).
    base_damage: dict[str, tuple[int, int]] = {}
    if is_weapon:
        ratio = ATKSPEED_CRAFT_RATIO[atk_speed]
        n_low = int((recipe.base_low * matmult) * ratio)
        n_high = int((recipe.base_high * matmult) * ratio)
        # The canonical damage range used downstream is 0.9x .. 1.1x of nDamBaseHigh.
        base_damage["neutral"] = (int(n_high * 0.9), int(n_high * 1.1))
        for e in SKP_ELEMENTS:
            base_damage[e] = (0, 0)

    # 3. Effectiveness grid.
    eff = _effectiveness_grid(ingredients)

    # 4. Aggregate ingredient ids + itemIDs + skill reqs (craft.js:462-491, 506-510).
    ids: dict[str, float] = {}
    skill_reqs: dict[str, int] = {s: 0 for s in SKP_ORDER}
    sp_gains: dict[str, int] = {s: 0 for s in SKP_ORDER}

    for n, ing in enumerate(ingredients):
        eff_mult = eff[n] / 100.0
        # itemIDs: skill requirements + durability. Requirements scale with eff.
        for key, val in ing.item_ids.items():
            if key == "dura" or val == 0:
                continue
            # "strReq" -> "strength"; map short names
            stat = {
                "strReq": "strength", "dexReq": "dexterity",
                "intReq": "intelligence", "defReq": "defence",
                "agiReq": "agility",
            }.get(key)
            if stat is None:
                continue
            # Requirements stack additively (craft.js:468 — rounds after scaling).
            skill_reqs[stat] += round(val * eff_mult)
        # Rolled ids (maxRolls used for our max-roll optimization).
        for short, max_val in ing.ids_max.items():
            if max_val == 0:
                continue
            scaled = max_val * eff_mult
            if short in ("str", "dex", "int", "def", "agi"):
                stat = {
                    "str": "strength", "dex": "dexterity", "int": "intelligence",
                    "def": "defence", "agi": "agility",
                }[short]
                sp_gains[stat] += int(round(scaled))
            mapped_key = INGREDIENT_ID_MAP.get(short, short)
            ids[mapped_key] = ids.get(mapped_key, 0.0) + scaled

    # Net skill requirement after the item itself grants some SP (craft.js logic
    # treats skill-point IDs as applying to the wearer once equipped).
    net_reqs = {
        stat: max(0, skill_reqs[stat] - sp_gains[stat])
        for stat in SKP_ORDER
    }

    # 5. Powder slots (weapons only).
    powder_slots = _powder_slots_for_level(recipe.level_min) if is_weapon else 0

    return CraftedItem(
        name=f"Crafted {recipe.name}",
        type=item_type,
        sub_type=sub_type,
        tier="crafted",
        level=recipe.level_max,
        class_req=None,
        skill_reqs={k: v for k, v in net_reqs.items() if v > 0},
        base_damage=base_damage,
        attack_speed=atk_speed if is_weapon else None,
        powder_slots=powder_slots,
        ids=ids,
        recipe_name=recipe.name,
        ingredient_names=[ing.name for ing in ingredients],
        material_tiers=material_tiers,
        crafted_atk_speed=atk_speed if is_weapon else None,
    )


# A sentinel "no ingredient" for partial builds.
def null_ingredient() -> Ingredient:
    return Ingredient(
        name="No Ingredient", tier=0, level=0, skills=[],
        ids_max={}, ids_min={},
        pos_mods={"left": 0, "right": 0, "above": 0, "under": 0,
                  "touching": 0, "notTouching": 0},
        item_ids={"dura": 0, "strReq": 0, "dexReq": 0, "intReq": 0,
                  "defReq": 0, "agiReq": 0},
    )
